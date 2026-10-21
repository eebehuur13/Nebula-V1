"""The Nebula MCP server: the tools Claude sees, sign-in, and the HTTP routes around them.

Claude on the web ignores a connector's general instructions and only reads each tool's
name and description, so the guidance on when and how to use Nebula lives in the tool
descriptions below. Keep them accurate; they are the product's manual for Claude.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from typing import Annotated, Any, Literal, Sequence

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult
from mcp_types import ToolAnnotations
from pydantic import Field

from nebula import access, db, extensions, files, jobs, limits, org, skills, storage
from nebula.access import AccessDenied, Person
from nebula.config import settings
from nebula.knowledge import datasets, items, links, notes, render, search as search_mod

log = logging.getLogger(__name__)

# Identity ------------------------------------------------------------------------------------

_person_cache: dict[str, tuple[float, Person]] = {}


async def me() -> Person:
    """The signed-in person for this request (cached for a minute)."""
    s = settings()
    if s.auth_mode == "dev":
        email, name, sub = s.dev_user_email, "Dev User", None
    else:
        token = get_access_token()
        claims = (token.claims if token else {}) or {}
        upstream = claims.get("upstream_claims") or {}
        email = claims.get("email") or upstream.get("email")
        name = claims.get("name") or upstream.get("name") or ""
        sub = upstream.get("sub") or claims.get("sub")
        if not email:
            raise ToolError("Nebula couldn't tell who you are. Reconnect the Nebula connector and sign in again.")
        verified = claims.get("email_verified", upstream.get("email_verified"))
        if verified is not True and verified != "true":
            raise ToolError("Nebula needs a verified Google email address. Reconnect after verifying it with Google.")
    email = email.lower()
    if not s.email_allowed(email):
        raise ToolError(f"Nebula is for Rocket Learning accounts; {email} isn't allowed. Sign in with your RL account.")
    hit = _person_cache.get(email)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        person = await access.ensure_person(email, name, sub)
    except AccessDenied as e:
        raise ToolError(str(e)) from e
    _person_cache[email] = (time.time() + 60, person)
    return person


def forget_person(email: str | None = None) -> None:
    if email:
        _person_cache.pop(email.lower(), None)
    else:
        _person_cache.clear()


def _out(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=_default, indent=1)


def _default(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return str(v)


def _fail(e: Exception) -> ToolError:
    if isinstance(e, ToolError):
        return e
    if isinstance(e, (AccessDenied, ValueError, notes.Conflict, datasets.QueryError, PermissionError)):
        return ToolError(str(e))
    log.exception("tool failed")
    return ToolError(f"Nebula hit an internal error ({type(e).__name__}). Try again; if it persists, tell an admin.")


READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
SENSITIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

mcp = FastMCP(name="Nebula", version="0.1.0")


# Tool visibility: admin tools for leads/admins, gap tools per their groups -----------------------

_gap_tools: dict[str, extensions.GapTool] = {}


def _visible_to(person: Person, tool_name: str) -> bool:
    if tool_name == "admin":
        return person.is_admin or person.is_lead
    gt = _gap_tools.get(tool_name)
    if gt:
        return (person.is_admin or ("all_rl" in gt.groups and person.is_rl_member)
                or bool(set(gt.groups) & set(person.groups)))
    return True


# Actions that only read, so they don't count against the daily write limit.
READ_ACTIONS = {
    "skills": {"find", "get"},
    "file": {"download", "status"},
    "sources": {"list", "status"},
    "admin": {"list_collections", "review", "recent", "export"},
}


def _counts_as_write(name: str, args: dict[str, Any]) -> bool:
    gt = _gap_tools.get(name)
    if gt:
        return gt.access != "read"
    if name == "save":
        return True
    if name == "share":
        return bool(args.get("add") or args.get("remove"))
    if name in READ_ACTIONS:
        return args.get("action") not in READ_ACTIONS[name]
    return False


def _bounded(result: Any) -> Any:
    """Keep any tool's text under the limit Claude can take in one result, and say so when cutting."""
    limit = settings().max_result_chars
    content = getattr(result, "content", None)
    if not content or sum(len(getattr(c, "text", "") or "") for c in content) <= limit:
        return result
    out, used = [], 0
    for c in content:
        text = getattr(c, "text", None)
        if text is None:
            out.append(c)
            continue
        room = max(0, limit - used)
        if len(text) > room:
            text = text[:room] + (f"\n\n[Nebula cut this result at {limit:,} characters. Ask for less: a page or part, "
                                  "a smaller limit, or fewer rows.]")
            c = c.model_copy(update={"text": text})
        used += len(text)
        out.append(c)
    return ToolResult(content=out, structured_content=getattr(result, "structured_content", None),
                      meta=getattr(result, "meta", None))


class VisibilityMiddleware(Middleware):
    async def on_list_tools(self, context: MiddlewareContext, call_next) -> Sequence[Tool]:
        tools = await call_next(context)
        try:
            person = await me()
        except ToolError:
            return [t for t in tools if t.name not in _gap_tools and t.name != "admin"]
        return [t for t in tools if _visible_to(person, t.name)]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = getattr(context.message, "name", "")
        args = getattr(context.message, "arguments", None) or {}
        person = await me()
        try:
            await limits.consume(person.id, "tool_calls")
            if _counts_as_write(name, args):
                await limits.consume(person.id, "writes")
        except limits.LimitExceeded as e:
            raise ToolError(str(e)) from e
        if name == "admin" or name in _gap_tools:
            if not _visible_to(person, name):
                raise ToolError("That tool isn't available to you.")
        return _bounded(await call_next(context))


mcp.add_middleware(VisibilityMiddleware())


# Tools -----------------------------------------------------------------------------------------


@mcp.tool(annotations=READ, output_schema=None)
async def search(
    query: Annotated[str, Field(description="What to look for, in plain words. Questions are fine.")],
    collection: Annotated[str | None, Field(description="Limit to one collection (slug or id).")] = None,
    types: Annotated[list[Literal["page", "record", "item", "dataset"]] | None,
                     Field(description="Limit to kinds: page (summary pages), record (decisions, facts, metrics, "
                                       "lessons), item (synced documents, sheets, slides, meetings, uploads), "
                                       "dataset (spreadsheet tables you can query with SQL).")] = None,
    since: Annotated[str | None, Field(description="Only material dated on/after this day (YYYY-MM-DD).")] = None,
    limit: Annotated[int, Field(ge=1, le=25)] = 8,
    include_superseded: Annotated[bool, Field(description="Also return records that were corrected/replaced.")] = False,
) -> str:
    """Search Nebula, Rocket Learning's memory: team pages, decisions, facts, metric definitions, lessons,
    the person's own saved notes and uploads, and the Google Drive / Notion / Granola files and spreadsheet
    datasets shared with them.

    Use this FIRST for any question about RL — its programmes, states, partners, teams, past decisions,
    numbers, processes or people's work — instead of answering from memory or general knowledge.
    Also use it first whenever the person mentions a document, file, note, page or meeting by name, or asks
    whether you have, can see or can find something: search Nebula before saying you don't have it.
    If the material may be in Hindi or another Indian language, search again in that language too.

    Results carry ids like 'page:…', 'record:…', 'item:…', 'dataset:…'. A short file comes back whole
    (`complete: true`). Everything else comes back as short excerpts, so `open` it before summarising,
    retelling or answering about the whole thing. Use `query` for numbers in a dataset. In your answer, cite
    what you used (title and date), and say when something is a draft, unverified, superseded or past its
    review date. If nothing relevant comes back, say Nebula doesn't have it rather than guessing. Don't use
    Nebula for general knowledge or for work you can do yourself (writing, analysis, making documents,
    charts or files)."""
    try:
        person = await me()
        since_d = dt.date.fromisoformat(since) if since else None
        res = await search_mod.search(person, query, collection=collection, types=list(types) if types else None,
                                      limit=limit, since=since_d, include_superseded=include_superseded)
        return _out(res)
    except Exception as e:
        raise _fail(e) from e


@mcp.tool(annotations=READ, output_schema=None, meta={"anthropic/maxResultSizeChars": 120_000})
async def open(  # noqa: A001 - the tool is called "open" on purpose
    id: Annotated[str, Field(description="An id from search, e.g. 'page:…', 'record:…', 'item:…', 'dataset:…'.")],
    part: Annotated[str | None, Field(description="For sources: 'page:3', 'pages:3-5', 'slide:5', 'sheet:Q2' or "
                                                  "'section:Heading text'.")] = None,
    page: Annotated[int, Field(ge=1, description="Which chunk of a long result to return.")] = 1,
    versions: Annotated[bool, Field(description="List earlier versions instead of the content.")] = False,
    as_image: Annotated[bool, Field(description="Items: return `part` ('page:3' of a PDF, 'slide:5', or 'image:2') as a "
                                                "picture, to read what text extraction missed.")] = False,
) -> str | list:
    """Read one Nebula item with its structure intact.

    - page: the summary page with status, owner, review date, and links in and out (follow them with open)
    - record: a decision/fact/metric/lesson/correction with its dates, evidence and what it superseded
    - item: a synced or uploaded source — documents by page, slides by number, sheets as tables (with the
      dataset id to query), meetings with timestamps
    - dataset: its card, columns and types, and sample rows (use `query` for numbers)

    Long items come in chunks; pass `page` to continue. Quote and cite precisely (title, page/slide/section,
    date). To work on the original file in your sandbox, use `file` with action 'download' instead.

    If an item lists `extraction.parts_not_read` (scanned pages, pictures, charts) and those parts matter for
    the question, open each with `part` and as_image=true, read it, and save what it says with save
    kind='transcription', so the next person finds it in search."""
    try:
        person = await me()
        kind, rid = links.parse_ref(id)
        await access.require(person, kind, rid, "view")
        size = 40_000
        if kind == "item" and as_image:
            return await _open_item_image(rid, part)
        if kind == "item":
            return await _open_item(rid, part, page, versions, size)
        if kind == "page":
            return await _open_page(rid, page, versions, size, person)
        if kind == "record":
            return await _open_record(rid, versions, person)
        if kind == "dataset":
            return await _open_dataset(rid)
        raise ValueError("open works for page, record, item and dataset ids.")
    except Exception as e:
        raise _fail(e) from e


async def _open_item(rid: str, part: str | None, page: int, versions: bool, size: int) -> str:
    it = await items.get(rid)
    assert it is not None
    if versions:
        vs = await db.fetch_all(
            "select version, title, source_modified_at, created_at from item_versions where item_id=%s order by version desc",
            (rid,),
        )
        out = {"id": f"item:{rid}", "versions": vs}
        if it["collection_kind"] != "notebook":
            out["note"] = ("Nebula keeps only the current copy of synced files; earlier versions are listed for "
                           "reference. Their content is in the source's own version history.")
        return _out(out)
    head = {
        "id": f"item:{rid}", "title": it["title"], "kind": it["kind"], "collection": it["collection_name"],
        "url": it["url"], "path": " / ".join(it["path"] or []), "author": it["author"],
        "modified": it["source_modified_at"], "version": it["version"], "status": it["status"],
    }
    if it["status_note"]:
        head["note"] = it["status_note"]
    ds = await db.fetch_all("select id, sheet, table_name, row_count from datasets where item_id=%s", (rid,))
    if ds:
        head["datasets"] = [{"id": f"dataset:{d['id']}", "sheet": d["sheet"], "table": f"data.{d['table_name']}",
                             "rows": d["row_count"]} for d in ds]
    doc = items.document_of(it)
    if doc is None:
        return _out({**head, "content": None, "hint": "No readable text; use file action 'download' to get the file."})
    text, label = render.document_markdown(doc, part)
    done = await items.transcriptions(None, rid)
    extraction = items.extraction_summary(doc, done)
    if extraction:
        head["extraction"] = extraction
    shown = [t for t in done if render.part_matches(t["part"], part)]
    if shown:
        text += "\n\n## Read from pictures by Claude\n\n" + "\n\n".join(
            f"### {items.part_label(t['part'])} (added by {t['added_by'] or 'someone'}, {t['created_at']:%Y-%m-%d})\n"
            f"{t['text']}" for t in shown)
    chunk, total = render.paginate(text, page, size)
    head["showing"] = (label + " · " if label else "") + f"chunk {min(page, total)} of {total}"
    if doc.metadata:
        head["metadata"] = {k: v for k, v in doc.metadata.items() if k in
                            ("page_count", "slide_count", "sheet_names", "needs_ocr", "word_count", "meeting_date",
                             "attendees")}
    return _out(head) + "\n\n" + chunk


async def _open_item_image(rid: str, part: str | None) -> list:
    from fastmcp.utilities.types import Image

    from nebula.ingest import images

    if not part:
        raise ValueError("Give the part to show, e.g. 'page:3', 'slide:5' or 'image:2'.")
    it = await items.get(rid)
    assert it is not None
    if not it["file_key"]:
        raise ValueError("Nebula doesn't hold the original of this item, so it can't show it as a picture.")
    data = await storage.get(it["file_key"])
    try:
        pictures = await asyncio.to_thread(images.render_part, data, it["filename"] or it["file_key"], part)
    except images.NoImage as e:
        raise ValueError(str(e)) from e
    except Exception as e:
        raise ValueError(f"Nebula couldn't render {part} of this file ({type(e).__name__}).") from e
    head = {"id": f"item:{rid}", "title": it["title"], "version": it["version"], "part": part,
            "pictures": len(pictures),
            "next": "Read what the picture says. To keep it, save it with save kind='transcription', id, part, "
                    f"expected_version={it['version']} and the text as body (transcribe; don't summarise)."}
    return [_out(head), *(Image(data=b, format=fmt) for b, fmt in pictures)]


async def _open_page(rid: str, page: int, versions: bool, size: int, person: Person) -> str:
    p = await db.fetch_one(
        """select p.*, c.name as collection, (select email from people where id=p.owner_person) as owner_email,
                  (select email from people where id=p.updated_by) as updated_by_email
           from pages p join collections c on c.id=p.collection_id where p.id=%s""",
        (rid,),
    )
    assert p is not None
    if versions:
        vs = await db.fetch_all(
            """select v.version, v.status, v.note, v.created_at, pe.email as edited_by from page_versions v
               left join people pe on pe.id=v.edited_by where v.page_id=%s order by v.version desc""",
            (rid,),
        )
        return _out({"id": f"page:{rid}", "versions": vs})
    nb = await links.neighbours(None, "page", rid, person=person)
    open_props = await db.fetch_one("select count(*) as n from proposals where page_id=%s and status='open'", (rid,))
    proposals = []
    if open_props and open_props["n"] and access.at_least(await access.level_for(person, "page", rid), "manage"):
        proposals = await db.fetch_all(
            """select pr.id, pr.note, pr.created_at, pe.email as proposed_by, pr.base_version from proposals pr
               left join people pe on pe.id = pr.proposed_by where pr.page_id=%s and pr.status='open'
               order by pr.created_at""", (rid,))
    chunk, total = render.paginate(p["body"], page, size)
    head = {
        "id": f"page:{rid}", "title": p["title"], "kind": p["kind"], "status": p["status"], "version": p["version"],
        "collection": p["collection"], "owner": p["owner_group"] or p["owner_email"], "aliases": p["aliases"],
        "updated": p["updated_at"], "updated_by": p["updated_by_email"], "review_by": p["review_by"],
        "stale": bool(p["review_by"] and p["review_by"] < dt.date.today()),
        "open_proposals": open_props["n"] if open_props else 0,
        **({"proposals_to_decide": [{"proposal_id": str(p["id"]), "by": p["proposed_by"], "note": p["note"],
                                     "proposed": p["created_at"], "based_on_version": p["base_version"]}
                                    for p in proposals],
            "how_to_decide": "Show the person the proposed edit, then save with kind='page', proposal_id and "
                             "accept=true or false."} if proposals else {}),
        "links_out": [{"id": f"{n['type']}:{n['id']}", "title": n["title"], "rel": n["rel"]} for n in nb["outgoing"]],
        "linked_from": [{"id": f"{n['type']}:{n['id']}", "title": n["title"], "rel": n["rel"]} for n in nb["incoming"]],
        "showing": f"chunk {min(page, total)} of {total}",
    }
    return _out(head) + "\n\n" + chunk


async def _open_record(rid: str, versions: bool, person: Person) -> str:
    r = await db.fetch_one(
        """select r.*, c.name as collection, (select email from people where id=r.author) as author_email
           from records r join collections c on c.id=r.collection_id where r.id=%s""",
        (rid,),
    )
    assert r is not None
    if versions:
        vs = await db.fetch_all(
            "select version, status, title, created_at from record_versions where record_id=%s order by version desc",
            (rid,),
        )
        return _out({"id": f"record:{rid}", "versions": vs})
    nb = await links.neighbours(None, "record", rid, person=person)
    return _out({
        "id": f"record:{rid}", "type": r["type"], "title": r["title"], "status": r["status"],
        "version": r["version"], "collection": r["collection"], "author": r["author_email"],
        "owner": r["owner_group"], "valid_from": r["valid_from"], "valid_to": r["valid_to"],
        "body": r["body"], "data": r["data"], "source_note": r["source_note"],
        "supersedes": f"record:{r['supersedes']}" if r["supersedes"] else None,
        "superseded_by": f"record:{r['superseded_by']}" if r["superseded_by"] else None,
        "evidence_and_links": [{"id": f"{n['type']}:{n['id']}", "title": n["title"], "rel": n["rel"]}
                               for n in nb["outgoing"]],
        "referenced_by": [{"id": f"{n['type']}:{n['id']}", "title": n["title"], "rel": n["rel"]} for n in nb["incoming"]],
        "updated": r["updated_at"],
    })


async def _open_dataset(rid: str) -> str:
    d = await db.fetch_one(
        "select d.*, i.title as item_title, i.url from datasets d left join items i on i.id=d.item_id where d.id=%s",
        (rid,),
    )
    assert d is not None
    sample = await db.fetch_all(f'select * from data."{d["table_name"]}" limit 5')  # noqa: S608
    return _out({
        "id": f"dataset:{rid}", "title": d["title"], "table": f"data.{d['table_name']}", "rows": d["row_count"],
        "columns": d["columns"], "source": {"title": d["item_title"], "url": d["url"]}, "card": d["card"],
        "sample_rows": sample, "updated": d["updated_at"],
        "hint": "Use the query tool with SQL against this table. Column names are the 'name' fields above.",
    })


@mcp.tool(annotations=READ, output_schema=None)
async def query(
    sql: Annotated[str, Field(description="One read-only PostgreSQL SELECT over data.<table> dataset tables.")],
    max_rows: Annotated[int, Field(ge=1, le=2000)] = 200,
) -> str:
    """Answer number questions from RL's spreadsheet data with SQL (PostgreSQL, read-only).

    Find datasets with `search` (type 'dataset' results show a table like data.t_…), then `open` the dataset to
    see its columns, types and sample rows before writing SQL. Use this for counts, sums, averages, trends and
    comparisons by state, district, programme or period — never estimate numbers from text snippets.
    Search for the metric's definition record first so the number matches RL's agreed definition, and state
    the period, filters and source table in your answer."""
    try:
        person = await me()
        return _out(await datasets.run_query(person, sql, max_rows))
    except Exception as e:
        raise _fail(e) from e


@mcp.tool(annotations=WRITE, output_schema=None)
async def save(
    kind: Annotated[Literal["record", "page", "transcription"],
                    Field(description="'record' for a small fact, 'page' for a summary page, 'transcription' for what a "
                                      "part of a source says that text extraction missed.")],
    title: Annotated[str | None, Field(description="Short, specific title (new pages and records).")] = None,
    body: Annotated[str | None, Field(description="Markdown. Pages: link other pages with [[Page title]] and cite "
                                                  "sources inline as nebula:item:<id> (add #page=3 etc.).")] = None,
    type: Annotated[Literal["decision", "fact", "metric", "lesson", "correction", "action", "note"] | None,
                    Field(description="Record type (records only).")] = None,
    page_kind: Annotated[Literal["programme", "state", "partner", "team", "process", "metric", "glossary", "person",
                                 "topic", "handover", "template", "other"] | None,
                         Field(description="What the page is about (pages only).")] = None,
    id: Annotated[str | None, Field(description="To update an existing page/record: its id.")] = None,
    expected_version: Annotated[int | None, Field(description="The version you read (required when updating).")] = None,
    collection: Annotated[str | None, Field(description="Where to save (slug). Default: the person's team notebook.")] = None,
    aliases: Annotated[list[str] | None, Field(description="Pages: other names and abbreviations, e.g. ['AP'].")] = None,
    data: Annotated[dict[str, Any] | None, Field(description="Records: structured fields. For a metric definition: "
                                                              "{definition, sql, source_table, unit, owner}.")] = None,
    valid_from: Annotated[str | None, Field(description="Records: date it became true (YYYY-MM-DD).")] = None,
    valid_to: Annotated[str | None, Field(description="Records: date it stopped being true.")] = None,
    supersedes: Annotated[str | None, Field(description="Records: the record:<id> this corrects or replaces.")] = None,
    evidence: Annotated[list[str] | None, Field(description="Ids of the sources this is based on.")] = None,
    status: Annotated[Literal["draft", "verified"] | None,
                      Field(description="'verified' only if the person is the owner/lead and confirms it.")] = None,
    note: Annotated[str | None, Field(description="Short note on what changed (updates).")] = None,
    confirm: Annotated[bool, Field(description="Save even though similar items exist.")] = False,
    part: Annotated[str | None, Field(description="Transcriptions: the part, e.g. 'page:3', 'slide:5', 'image:2'.")] = None,
    proposal_id: Annotated[str | None, Field(description="Pages: a proposed edit to accept or reject (see open).")] = None,
    accept: Annotated[bool | None, Field(description="With proposal_id: true to accept the edit, false to reject.")] = None,
) -> str:
    """Save knowledge so others at RL can find it later — only when the person asks or agrees. Offer to save
    decisions, corrections and lessons when a piece of work produces them.

    kind 'record' = one small fact: type decision | fact | metric (a metric definition: put what it counts,
    the SQL and source table in `data`) | lesson | correction (set `supersedes` to the record it corrects) |
    action | note. kind 'page' = a readable summary page about one thing (a programme, state, partner, team,
    process, metric, glossary term), in markdown, citing its sources.

    For a new item Nebula first returns similar existing ones; if one already covers it, update that one
    (pass `id` and the `expected_version` you read) instead of duplicating, otherwise call again with
    confirm=true. Edits to a verified page by someone who doesn't own it become a proposal for the owner.
    Saves go to the person's team notebook unless `collection` is given.

    kind 'transcription' = what one part of a source says that text extraction missed (a scanned page, a
    chart, a photo of a table), after you looked at it with open as_image=true: pass the item `id`, `part`,
    the `expected_version` you read and the text as `body`. Transcribe faithfully; it's indexed with the file
    for everyone who can open it.

    The owner of a verified page decides edits others proposed: open the page to see them, then save with
    kind='page', `proposal_id` and accept=true or false (after asking the person)."""
    try:
        person = await me()
        if proposal_id:
            if accept is None:
                raise ValueError("Say whether to accept the proposed edit (accept=true) or reject it (accept=false).")
            async with db.tx() as conn:
                return _out(await notes.decide_proposal(conn, person, proposal_id, accept))
        if kind == "transcription":
            if not (id and part and body):
                raise ValueError("A transcription needs the item id, the part and the text (body).")
            k, rid = links.parse_ref(id)
            if k != "item":
                raise ValueError("Transcriptions belong to a source item (item:…).")
            async with db.tx() as conn:
                return _out(await items.save_transcription(conn, person, rid, part, body, expected_version))
        vf = dt.date.fromisoformat(valid_from) if valid_from else None
        vt = dt.date.fromisoformat(valid_to) if valid_to else None
        if id:
            k, rid = links.parse_ref(id)
            async with db.tx() as conn:
                if k == "page":
                    res = await notes.update_page(conn, person, rid, expected_version=expected_version, title=title,
                                                  body=body, aliases=aliases, status=status, kind=page_kind,
                                                  note=note or "")
                    if res.get("proposed"):
                        return _out({"saved": False, "proposed": True, "proposal_id": res["proposal_id"],
                                     "message": "This page is verified, so your edit was sent to its owner as a "
                                                "proposal. They'll see it in their review queue."})
                    return _out(_saved("page", res))
                if k == "record":
                    res = await notes.update_record(conn, person, rid, expected_version=expected_version, title=title,
                                                    body=body, data=data, status=status, valid_from=vf, valid_to=vt)
                    return _out(_saved("record", res))
            raise ValueError("Only pages and records can be updated with save.")
        if not title:
            raise ValueError("A new page or record needs a title.")
        if not confirm:
            similar = await search_mod.search(person, f"{title}\n{(body or '')[:500]}", types=[kind], limit=4)
            close = [r for r in similar.get("results", []) if r.get("score", 0) >= _similar_threshold()]
            if close:
                return _out({
                    "saved": False, "similar": [{k: r.get(k) for k in ("id", "title", "type", "status", "date")} for r in close],
                    "next": "If one of these already covers it, open it and update it (save with id + expected_version). "
                            "Otherwise call save again with confirm=true.",
                })
        async with db.tx() as conn:
            col = await notes.notebook_for(conn, person, collection)
            if kind == "page":
                if not body:
                    raise ValueError("A page needs a body.")
                res = await notes.create_page(conn, person, collection_id=str(col["id"]), title=title, body=body,
                                              kind=page_kind or "topic", aliases=aliases, status=status or "draft",
                                              review_days=col["review_days"])
            else:
                if not type:
                    raise ValueError("A record needs a type (decision, fact, metric, lesson, correction, action, note).")
                res = await notes.create_record(conn, person, collection_id=str(col["id"]), type=type, title=title,
                                                body=body or "", data=data, status=status or "draft", valid_from=vf,
                                                valid_to=vt, supersedes=links.parse_ref(supersedes)[1] if supersedes else None,
                                                evidence=evidence)
                for ref in evidence or []:
                    k2, i2 = links.parse_ref(ref)
                    await access.require(person, k2, i2, "view", conn)
            return _out({**_saved(kind, res), "collection": col["name"]})
    except Exception as e:
        raise _fail(e) from e


def _similar_threshold() -> float:
    return 0.5 if settings().rerank_provider != "none" else 0.03


def _saved(kind: str, res: dict) -> dict:
    out = {"saved": True, "id": f"{kind}:{res['id']}", "version": res["version"], "status": res["status"],
           "web": f"{settings().public_url}/{kind}/{res['id']}"}
    if res.get("unresolved"):
        out["unresolved_links"] = res["unresolved"]
        out["hint"] = "These [[links]] don't match a page yet; create the pages or fix the titles."
    if res["status"] == "draft":
        out["note"] = "Saved as a draft; the owner or a team lead can verify it."
    return out


@mcp.tool(name="skills", annotations=WRITE, output_schema=None)
async def skills_tool(
    action: Annotated[Literal["find", "get", "save", "publish", "retire", "feedback"], Field(description="What to do.")],
    name: Annotated[str | None, Field(description="Skill name (lowercase-with-hyphens).")] = None,
    task: Annotated[str | None, Field(description="find: a short description of the task at hand.")] = None,
    file: Annotated[str | None, Field(description="get: a file path inside the skill to read.")] = None,
    skill_md: Annotated[str | None, Field(description="save: full SKILL.md including YAML frontmatter "
                                                      "(name, description).")] = None,
    files: Annotated[dict[str, str] | None, Field(description="save: extra text files {path: content}, e.g. templates.")] = None,
    owner: Annotated[str | None, Field(description="save: owning team id (default: you).")] = None,
    share_with: Annotated[list[str] | None, Field(description="publish: extra group ids that may use it.")] = None,
    version: Annotated[int | None, Field(description="get/publish: a specific version.")] = None,
    comment: Annotated[str | None, Field(description="feedback: what worked or didn't.")] = None,
) -> str:
    """RL's shared skills: written methods for recurring RL work (reports, updates, proposals, reviews…) in the
    Agent Skills format (SKILL.md plus files).

    Before doing a recurring RL task, call action 'find' with a short description of the task. If a skill
    fits, 'get' it and follow its SKILL.md; read files it references with 'get' + `file`. Skills that tell
    you to use your own abilities (writing, files, charts) mean exactly that — Nebula only supplies RL's
    knowledge, data, sharing and gap tools.

    People can turn a way of working into a skill: 'save' with name, skill_md and optional text files. It stays
    a draft until a lead of the owning team publishes it ('publish'). Use 'feedback' to record what worked."""
    try:
        person = await me()
        if action == "find":
            rows = await skills.visible(person, task)
            return _out({"skills": [{"name": r["name"], "description": r["description"], "status": r["status"],
                                     "owner": r["owner_group"] or "personal", "version": r["current_version"]}
                                    for r in rows]} if rows else {"skills": [], "note": "No RL skill for this yet."})
        if not name:
            raise ValueError("Give the skill name.")
        if action == "get":
            if file:
                return _out(await skills.get_file(person, name, file))
            d = await skills.get(person, name, version)
            return _out({"name": name, "status": d["skill"]["status"], "version": d["version"]["version"],
                         "files": d["files"]}) + "\n\n" + d["version"]["skill_md"]
        if action == "save":
            if not skill_md:
                raise ValueError("Give skill_md (the full SKILL.md).")
            return _out(await skills.save(person, name=name, skill_md=skill_md, files=files, owner=owner))
        if action == "publish":
            return _out(await skills.publish(person, name, version, share_with))
        if action == "retire":
            return _out(await skills.retire(person, name))
        if action == "feedback":
            return _out(await skills.feedback(person, name, comment or ""))
        raise ValueError("Unknown action.")
    except Exception as e:
        raise _fail(e) from e


@mcp.tool(annotations=WRITE, output_schema=None)
async def file(
    action: Annotated[Literal["download", "upload", "status"], Field(description="What to do.")],
    id: Annotated[str | None, Field(description="download: the item/page/record/dataset id.")] = None,
    filename: Annotated[str | None, Field(description="upload: the file's name, with extension.")] = None,
    collection: Annotated[str | None, Field(description="upload: notebook collection to store it in "
                                                        "(default: the person's team notebook).")] = None,
    title: Annotated[str | None, Field(description="upload: a human title.")] = None,
    replaces: Annotated[str | None, Field(description="upload: item:<id> of an earlier upload this is a new "
                                                      "version of.")] = None,
    upload_id: Annotated[str | None, Field(description="status: the upload_id from the upload step.")] = None,
    inline: Annotated[bool, Field(description="download: return small files (≤100 KB) as base64 instead of a link.")] = False,
) -> str:
    """Move files between your code sandbox and Nebula.

    - 'download': a short-lived link to an item's original file (or a markdown/CSV export of a page, record or
      dataset). Fetch it from your sandbox: curl -fsSL -o <name> '<url>'. Do the real work on the file with
      your own tools.
    - 'upload': get a link for a finished file, then PUT it from your sandbox: curl -fsS -X PUT
      --data-binary @<file> '<url>'. Nebula stores it in the collection and makes it searchable.
    - 'status': check an upload.
    The sandbox must be allowed to reach the Nebula domain (Settings → Capabilities → Code execution →
    allowed domains). If curl can't connect, use inline=true for small downloads, or tell the person to add
    the domain."""
    try:
        person = await me()
        if action == "download":
            if not id:
                raise ValueError("Give the id to download.")
            return _out(await files.download(person, id, inline))
        if action == "upload":
            if not filename:
                raise ValueError("Give the filename.")
            return _out(await files.start_upload(person, filename, collection, title, replaces))
        if action == "status":
            if not upload_id:
                raise ValueError("Give the upload_id.")
            return _out(await files.status(person, upload_id))
        raise ValueError("Unknown action.")
    except Exception as e:
        raise _fail(e) from e


@mcp.tool(annotations=SENSITIVE, output_schema=None)
async def share(
    id: Annotated[str, Field(description="Item, page, record, dataset or collection id ('collection:<slug or id>'), "
                                         "or 'skill:<name>'.")],
    add: Annotated[list[dict[str, str]] | None,
                   Field(description="People or groups to add: [{\"who\": \"email or group id\", "
                                     "\"level\": \"view|edit|manage\"}]. 'all_rl' is everyone at RL.")] = None,
    remove: Annotated[list[str] | None, Field(description="Emails or group ids to remove.")] = None,
) -> str:
    """See or change who can access something in Nebula. Without add/remove it shows the current access.

    Levels: view, edit, manage (manage = can change sharing). Groups are RL teams (use `people` to list them);
    'all_rl' is everyone at RL and needs a team lead. Confirm the change with the person before calling, and
    tell them afterwards who has access now."""
    try:
        person = await me()
        rtype, rid = await _resolve_shareable(id)
        if not add and not remove:
            await access.require(person, rtype, rid, "view")
            return _out(await org.access_list(None, rtype, rid))
        res = await org.share(person, rtype, rid, add, remove)
        forget_person()
        return _out(res)
    except Exception as e:
        raise _fail(e) from e


async def _resolve_shareable(ref: str) -> tuple[str, str]:
    ref = ref.strip().removeprefix("nebula:")
    if ref.startswith("skill:"):
        s = await db.fetch_one("select id from skills where name=%s", (ref.split(":", 1)[1],))
        if not s:
            raise ValueError("No such skill.")
        return "skill", str(s["id"])
    if ref.startswith("collection:"):
        key = ref.split(":", 1)[1]
        c = await db.fetch_one("select id from collections where slug=%s or id::text=%s", (key, key))
        if not c:
            raise ValueError("No such collection.")
        return "collection", str(c["id"])
    return links.parse_ref(ref)


@mcp.tool(annotations=READ, output_schema=None)
async def people(
    query: Annotated[str | None, Field(description="Find people by name or email.")] = None,
    email: Annotated[str | None, Field(description="One person's teams, roles and what they own.")] = None,
    groups: Annotated[bool, Field(description="List all groups with their members and leads.")] = False,
    role: Annotated[str | None, Field(description="Who held a role over time (role id).")] = None,
) -> str:
    """Who's who at RL in Nebula: find people, list teams (groups) with their leads and members, see one
    person's teams, roles and owned work, or who held a role over time ("who led the programme in March?").
    Also tells you which group ids to use when sharing."""
    try:
        person = await me()
        if email:
            if email.strip().lower() != person.email and not person.is_admin:
                target = await access.get_person(email)
                if not person.is_rl_member or not target or not target.is_rl_member:
                    raise AccessDenied("You can only open your own profile or the RL directory.")
            return _out(await org.person_overview(email) or {"note": "No Nebula account with that email."})
        if role:
            if not person.is_rl_member:
                raise AccessDenied("The RL role directory is only available to RL members.")
            return _out({"role": role, "history": await org.role_history(role)})
        if groups or not query:
            directory = await org.groups_overview()
            if not person.is_rl_member:
                directory = [g for g in directory if g["id"] in person.groups]
            return _out({"groups": directory})
        return _out({"people": await org.find_people(query, viewer=person)})
    except Exception as e:
        raise _fail(e) from e


@mcp.tool(annotations=SENSITIVE, output_schema=None)
async def admin(
    action: Annotated[Literal["list_collections", "create_collection", "sync", "create_group", "set_group_email",
                              "add_member", "remove_member", "review", "recent", "decide_proposal", "verify",
                              "handover", "export"],
                      Field(description="What to do.")],
    group: Annotated[str | None, Field(description="Group id, e.g. 'ap'.")] = None,
    email: Annotated[str | None, Field(description="add_member/remove_member: the person's email.")] = None,
    role: Annotated[Literal["member", "lead"] | None, Field(description="add_member: member or lead.")] = None,
    name: Annotated[str | None, Field(description="create_group/create_collection: display name.")] = None,
    slug: Annotated[str | None, Field(description="create_collection: short id like 'ap-drive'.")] = None,
    source: Annotated[Literal["gdrive", "notion", "granola", "notebook"] | None,
                      Field(description="create_collection: where it comes from.")] = None,
    source_config: Annotated[dict[str, Any] | None,
                             Field(description="create_collection: gdrive {drive_id} or {folder_id}; notion "
                                               "{root_page_id, exclude_page_ids: [...]}; granola {space_ids: [...]} "
                                               "or {folder_ids: [...]}.")] = None,
    audience: Annotated[list[str] | None, Field(description="create_collection: groups that may view it besides "
                                                            "the owner group (never wider than who can open it in "
                                                            "the source).")] = None,
    collection: Annotated[str | None, Field(description="sync: collection slug.")] = None,
    full: Annotated[bool, Field(description="sync: full re-read instead of changes only.")] = False,
    proposal_id: Annotated[str | None, Field(description="decide_proposal: the proposal id.")] = None,
    accept: Annotated[bool | None, Field(description="decide_proposal: accept (true) or reject (false).")] = None,
    id: Annotated[str | None, Field(description="verify: page:<id> or record:<id>.")] = None,
    from_email: Annotated[str | None, Field(description="handover: person handing over.")] = None,
    to_email: Annotated[str | None, Field(description="handover: successor.")] = None,
    leaving: Annotated[bool, Field(description="handover: the person is leaving RL (deactivate them).")] = False,
    apply: Annotated[bool, Field(description="handover: carry it out (otherwise preview only).")] = False,
    description: Annotated[str | None, Field(description="create_group/create_collection: what it is.")] = None,
    google_group: Annotated[str | None, Field(description="create_group/set_group_email: the team's Google Group "
                                                          "address, so Drive files shared with it reach the team.")] = None,
    days: Annotated[int, Field(ge=1, le=90, description="recent: how many days back.")] = 7,
) -> str:
    """Team lead and admin actions in Nebula.

    list_collections · create_collection (register a Notion page tree or a Granola space for a team, or
    publish a whole Drive folder/Shared Drive to groups; for everyday Drive sharing people just share with
    Nebula, see the sources tool) · sync (refresh a collection now) ·
    create_group · set_group_email (link a team to its Google Group) · add_member / remove_member · review
    (pages past their review date, proposed edits, drafts
    waiting for verification, draft skills) · recent (what changed in the last `days` days; for the weekly
    upkeep — get the 'nebula-upkeep' skill first) · decide_proposal · verify (mark a page or record verified) ·
    handover (always preview first; then apply=true after the person confirms) · export (a collection's
    pages and records as an Obsidian-ready zip).
    Who sees a collection: Granola notes are visible to the people who were in each meeting; Notion pages to
    everyone in the owning team and `audience` (Notion's per-page permissions can't be read, so leave
    restricted pages out with exclude_page_ids); `audience` groups see everything in it.
    Confirm any change with the person before calling."""
    try:
        person = await me()
        if action == "list_collections":
            return _out(await org.list_collections(person))
        if action == "create_collection":
            if not (slug and name and source and group):
                raise ValueError("create_collection needs slug, name, source and group (the owning team).")
            return _out(await org.create_collection(person, slug=slug, name=name, kind=source,
                                                    source_config=source_config or {}, owner_group=group,
                                                    audience=audience, description=description or ""))
        if action == "sync":
            if not collection:
                raise ValueError("Give the collection slug.")
            return _out(await org.sync_now(person, collection, full))
        if action == "create_group":
            if not (group and name):
                raise ValueError("create_group needs group (id) and name.")
            res = await org.create_group(person, group, name, description or "", google_group_email=google_group,
                                         lead_email=email)
            forget_person()
            return _out(res)
        if action == "set_group_email":
            if not group:
                raise ValueError("set_group_email needs the group and its google_group address.")
            return _out(await org.set_group_email(person, group, google_group))
        if action == "add_member":
            if not (group and email):
                raise ValueError("add_member needs group and email.")
            res = await org.set_member(person, group, email, role or "member")
            forget_person(email)
            return _out(res)
        if action == "remove_member":
            if not (group and email):
                raise ValueError("remove_member needs group and email.")
            res = await org.remove_member(person, group, email)
            forget_person(email)
            return _out(res)
        if action == "review":
            return _out(await org.review_queue(person))
        if action == "recent":
            return _out(await org.recent_changes(person, days, collection))
        if action == "export":
            if not collection:
                raise ValueError("Give the collection slug to export.")
            return _out(await org.export_collection(person, collection))
        if action == "decide_proposal":
            if not proposal_id or accept is None:
                raise ValueError("decide_proposal needs proposal_id and accept.")
            async with db.tx() as conn:
                return _out(await notes.decide_proposal(conn, person, proposal_id, accept))
        if action == "verify":
            if not id:
                raise ValueError("Give the page or record id.")
            k, rid = links.parse_ref(id)
            async with db.tx() as conn:
                if k == "page":
                    res = await notes.update_page(conn, person, rid, expected_version=None, status="verified",
                                                  note="verified")
                elif k == "record":
                    res = await notes.update_record(conn, person, rid, expected_version=None, status="verified")
                else:
                    raise ValueError("Only pages and records can be verified.")
            return _out({"verified": id, "version": res["version"]})
        if action == "handover":
            if not (from_email and to_email):
                raise ValueError("handover needs from_email and to_email.")
            res = await org.handover(person, from_email=from_email, to_email=to_email, group=group,
                                     leaving=leaving, apply=apply)
            forget_person()
            return _out(res)
        raise ValueError("Unknown action.")
    except Exception as e:
        raise _fail(e) from e


def _sources_description() -> str:
    from nebula.connectors.gdrive import service_account_email

    bot = service_account_email() or "Nebula's Google service account"
    return f"""Bring Google Drive material into Nebula, refresh it, and check on it.

    Sharing is enough: when someone shares a Drive folder, a single file or a Shared Drive with
    {bot} as an Editor, Nebula notices within a few minutes, imports it and keeps it in step with
    Drive (edits show up within minutes). Editor is needed because Drive only shows who a file is
    shared with to editors; Nebula itself only reads. A link alone isn't enough: Drive has to share it
    with that address. If a result carries a `warning` or `note` about Viewer access, pass it on.

    Use this tool when the person wants something now or wants to know where it stands:
    - 'sync' with `link` (a Drive file, folder or Shared Drive link) or `id` (item:…): import or refresh it
      right away, e.g. "I've updated this doc, use the new version" or "save this file to Nebula". With
      `source` (from 'list'): re-check a whole shared folder.
    - 'status' with `link` or `id`: is this file in Nebula, up to date, searchable, and fully read? With
      `source`: a shared folder's progress.
    - 'connect' with `link`: the same as sync, for something not in Nebula yet.
    - 'list': the folders, files and drives this person has shared with Nebula.
    - 'disconnect' with `source`: stop indexing it (Drive itself is untouched).
    sync and connect return a job and a rough wait. Tell the person how long it should take, and don't
    treat the new version as available until the job (check it with the job tool) says done.

    Access follows Drive: each file is visible in Nebula only to the people Drive shares it with
    (people, Google Groups mapped to Nebula teams, and the RL domain), and changes when Drive sharing
    changes. Sharing a folder with Nebula never shows its files to anyone Drive doesn't already let
    open them."""


@mcp.tool(name="sources", annotations=WRITE, output_schema=None, description=_sources_description())
async def sources_tool(
    action: Annotated[Literal["list", "sync", "status", "connect", "disconnect", "connect_drive"],
                      Field(description="What to do.")],
    link: Annotated[str | None, Field(description="A Google Drive link (file, folder, Docs/Sheets/Slides or "
                                                  "Shared Drive) or id.")] = None,
    id: Annotated[str | None, Field(description="status/sync: a Nebula item id (item:…) for one file.")] = None,
    source: Annotated[str | None, Field(description="status/sync/disconnect: a source id from 'list' (or a "
                                                    "collection slug).")] = None,
    name: Annotated[str | None, Field(description="connect: a name for it (default: its name in Drive).")] = None,
) -> str:
    from nebula import sources

    try:
        person = await me()
        if action == "list":
            return _out(await sources.list_sources(person))
        if action in ("connect", "connect_drive"):
            if not link:
                raise ValueError("Give the Drive link.")
            return _out(await sources.connect(person, link, name))
        if action == "sync":
            return _out(await sources.request_sync(person, link=link, item=id, source=source))
        if action == "status":
            return _out(await sources.status(person, link=link, item=id, source=source))
        if action == "disconnect":
            if not source and not link:
                raise ValueError("Give the source (see action 'list').")
            return _out(await sources.disconnect(person, source or link or ""))
        raise ValueError("Unknown action.")
    except Exception as e:
        raise _fail(e) from e


@mcp.tool(annotations=READ, output_schema=None)
async def job(id: Annotated[str, Field(description="The job id.")]) -> str:
    """Check a long-running Nebula job (a sync, a file refresh, an upload being indexed, a gap-tool run):
    status, a rough wait while it's queued or running, and the result when it's done."""
    try:
        person = await me()
        j = await jobs.get(id)
        if not j or (not person.is_admin and (not j["person_id"] or str(j["person_id"]) != person.id)):
            raise ValueError("No such job of yours.")
        out = {k: j[k] for k in ("id", "kind", "status", "progress", "message", "result", "updated_at")}
        if j["status"] in ("queued", "running"):
            out.update(await jobs.eta(str(j["id"])))
        elif j["status"] == "failed":
            out["error"] = (j["error"] or "").split("\n", 1)[0][:500]
        return _out(out)
    except Exception as e:
        raise _fail(e) from e


# Gap tools from the registry ---------------------------------------------------------------------


class GapToolAdapter(Tool):
    """Exposes one gap tool from the registry as an ordinary MCP tool."""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        gt = _gap_tools[self.name]
        person = await me()
        try:
            if gt.long_running:
                job_id = await jobs.enqueue("gap_tool", {"tool": gt.id, "args": arguments, "person_id": person.id},
                                            person_id=person.id, priority=30)
                await org.audit(None, person, "gap_tool", "tool", gt.id, {"job": job_id})
                return ToolResult(content=_out({"job": job_id, "next": "Check progress with the job tool."}))
            result = await gt.handler(person, arguments)
            await org.audit(None, person, "gap_tool", "tool", gt.id, {})
            return ToolResult(content=_out(_with_links(result)))
        except Exception as e:
            raise _fail(e) from e


def _with_links(result: dict) -> dict:
    out = dict(result)
    if result.get("files"):
        out["files"] = [{"filename": f["filename"], "url": storage.signed_url(f["key"], method="GET",
                                                                          filename=f["filename"], mime=f.get("mime", ""),
                                                                          ttl=3600)}
                        for f in result["files"]]
    return out


@jobs.handler("gap_tool")
async def _gap_tool_job(job_row: dict, payload: dict) -> dict:
    gt = {t.id: t for t in extensions.discover()}.get(payload["tool"])
    if not gt:
        raise ValueError(f"gap tool {payload['tool']} is no longer live")
    person_row = await db.fetch_one("select email from people where id=%s", (payload["person_id"],))
    assert person_row is not None
    person = await access.get_person(person_row["email"])
    assert person is not None
    return _with_links(await gt.handler(person, payload["args"]))


def load_gap_tools() -> None:
    for gt in extensions.discover():
        _gap_tools[gt.id] = gt
        mcp.add_tool(GapToolAdapter(
            name=gt.id, description=gt.full_description, parameters=gt.input_schema,
            annotations=READ if gt.access == "read" else WRITE,
        ))
        log.info("loaded gap tool %s", gt.id)
