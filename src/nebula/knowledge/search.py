"""Hybrid search: meaning (vectors) + words (full text), fused, reranked, filtered to what
the person may see, then grouped per source with citations."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from nebula import access, db, models
from nebula.config import settings
from nebula.knowledge import items, render

STOPWORDS = [
    # English
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "were", "be", "been", "by",
    "with", "what", "which", "who", "whom", "how", "when", "where", "why", "did", "do", "does", "we", "our", "us",
    "it", "its", "this", "that", "these", "those", "about", "from", "at", "as", "any", "all", "can", "could",
    "should", "would", "will", "there", "their", "they", "i", "me", "my", "you", "your", "has", "have", "had",
    # Hindi
    "है", "हैं", "था", "थे", "थी", "का", "की", "के", "को", "में", "से", "पर", "और", "या", "यह", "वह", "जो", "तो",
    "कि", "भी", "क्या", "कैसे", "कब", "कहाँ", "कौन", "हम", "हमने",
]

RRF_K = 60
CANDIDATES = 40
# A file this short comes back whole, so Claude doesn't answer about it from two excerpts.
FULL_TEXT_CHARS = 4000
FULL_TEXT_BUDGET = 12000  # whole-file text per search, across all results
EXCERPT_NOTE = ("Matches are short excerpts, not whole documents. Open an id to read it in full before "
                "summarising, retelling or answering about the whole thing.")


@dataclass
class Candidate:
    chunk_id: int
    source_type: str
    source_id: str
    collection_id: str | None
    heading: str
    text: str
    locator: dict
    kind: str
    score: float = 0.0


async def _aliases_in(query: str, person: access.Person) -> list[dict]:
    rows = await db.fetch_all(
        "select id, title, aliases from pages where status <> 'archived' and cardinality(aliases) > 0"
    )
    q = query.lower()
    hits = []
    for r in rows:
        for a in r["aliases"]:
            if len(a) >= 2 and re.search(r"(?<!\w)" + re.escape(a.lower()) + r"(?!\w)", q):
                if access.at_least(await access.level_for(person, "page", str(r["id"])), "view"):
                    hits.append(r)
                break
    return hits


def _scope_sql(alias: str = "c") -> str:
    return f"""(
        {alias}.collection_id = any(%(cols)s::uuid[])
        or exists (select 1 from unnest(%(et)s::text[], %(ei)s::uuid[]) as e(t, i)
                   where e.t = {alias}.source_type and e.i = {alias}.source_id)
    ) and {alias}.source_type = any(%(types)s::text[])"""


async def search(
    person: access.Person,
    query: str,
    *,
    collection: str | None = None,
    types: list[str] | None = None,
    limit: int | None = None,
    since: dt.date | None = None,
    include_superseded: bool = False,
) -> dict:
    limit = max(1, min(limit or settings().search_default_limit, 25))
    types = [t for t in (types or ["page", "record", "item", "dataset"]) if t in ("page", "record", "item", "dataset", "skill")]
    cols, extras = await access.visible_scope(person)
    if collection:
        row = await db.fetch_one("select id from collections where slug=%s or id::text=%s", (collection, collection))
        if not row:
            return {"results": [], "note": f"No collection '{collection}' that you can see."}
        cid = str(row["id"])
        if cid in cols:
            cols, extras = [cid], []
        else:
            # Per-file source grants do not imply access to every file in the collection.
            granted = await db.fetch_all(
                """select distinct source_type, source_id from chunks c
                   where collection_id=%s and exists (
                       select 1 from unnest(%s::text[], %s::uuid[]) as e(t,i)
                       where e.t=c.source_type and e.i=c.source_id)""",
                (cid, [t for t, _ in extras], [rid for _, rid in extras]),
            )
            cols, extras = [], [(r["source_type"], str(r["source_id"])) for r in granted]
    if not cols and not extras:
        return {"results": [], "note": "Nothing in Nebula is shared with you yet. Save something, share a Drive "
                                       "folder or file with Nebula (see the sources tool), or ask a colleague to "
                                       "share with you."}

    alias_pages = await _aliases_in(query, person)
    # "AP" -> "AP (Andhra Pradesh)": expansion feeds every stage (vector, keyword, rerank).
    expanded = query + "".join(f" ({p['title']})" for p in alias_pages)

    params = {
        "cols": cols,
        "et": [t for t, _ in extras],
        "ei": [i for _, i in extras],
        "types": types,
        "k": CANDIDATES,
    }
    qvec = (await models.embed([expanded], "query"))[0]

    async with db.tx() as conn:
        await conn.execute("set local hnsw.iterative_scan = 'relaxed_order'")
        await conn.execute("set local hnsw.ef_search = 120")
        vec_rows = await db.fetch_all(
            f"""select id, source_type, source_id, collection_id, heading, text, locator, kind
                from chunks c where c.embedding is not null and {_scope_sql()}
                order by c.embedding <=> %(q)s::halfvec limit %(k)s""",
            {**params, "q": qvec},
            conn,
        )
        kw_rows = await db.fetch_all(
            f"""with q as (
                    select to_tsquery('simple', string_agg(quote_literal(lexeme), ' | ')) as tsq
                    from unnest(to_tsvector('simple', %(text)s)) where lexeme <> all(%(stop)s::text[])
                )
                select c.id, c.source_type, c.source_id, c.collection_id, c.heading, c.text, c.locator, c.kind
                from chunks c, q where q.tsq is not null and c.tsv @@ q.tsq and {_scope_sql()}
                order by ts_rank_cd(c.tsv, q.tsq, 1) desc limit %(k)s""",
            {**params, "text": expanded, "stop": STOPWORDS},
            conn,
        )

    fused: dict[int, Candidate] = {}
    for ranked in (vec_rows, kw_rows):
        for rank, r in enumerate(ranked):
            c = fused.get(r["id"])
            if c is None:
                c = fused[r["id"]] = Candidate(
                    r["id"], r["source_type"], str(r["source_id"]),
                    str(r["collection_id"]) if r["collection_id"] else None,
                    r["heading"], r["text"], r["locator"] or {}, r["kind"],
                )
            c.score += 1.0 / (RRF_K + rank + 1)
    pool = sorted(fused.values(), key=lambda c: c.score, reverse=True)[:30]
    if not pool:
        return {"results": [], "note": "Nothing found. Try other words (or the Hindi/English equivalent), or check the collection."}

    order = await models.rerank(expanded, [(c.heading + "\n" + c.text)[:2000] for c in pool], top_n=len(pool))
    reranked = [(pool[i], s) for i, s in order]

    meta = await _metadata({(c.source_type, c.source_id) for c, _ in reranked})
    alias_ids = {str(p["id"]) for p in alias_pages}
    today = dt.date.today()
    grouped: dict[tuple[str, str], dict] = {}
    for c, s in reranked:
        m = meta.get((c.source_type, c.source_id))
        if not m:
            continue
        if m.get("status") in ("deleted", "archived"):
            continue
        if m.get("status") == "superseded" and not include_superseded:
            continue
        if since and m.get("date") and str(m["date"]) < since.isoformat():
            continue
        boost = 1.0
        if m.get("status") == "verified":
            boost *= 1.15
        if c.source_type == "page" and c.source_id in alias_ids:
            boost *= 1.3
        key = (c.source_type, c.source_id)
        entry = grouped.get(key)
        if entry is None:
            entry = grouped[key] = {**m, "score": s * boost, "matches": []}
        if len(entry["matches"]) < 2:
            entry["matches"].append({"where": where_text(c.locator, c.heading), "text": _snippet(c.text)})
        if m.get("review_by") and m["review_by"] < today:
            entry["stale"] = f"review was due {m['review_by']}"
    results = sorted(grouped.values(), key=lambda e: e["score"], reverse=True)[:limit]
    for r in results:
        r.pop("review_by", None)
        r["score"] = round(r["score"], 3)
    await _inline_short_items(results)
    if any("matches" in r for r in results):
        return {"note": EXCERPT_NOTE, "results": results}
    return {"results": results}


async def _inline_short_items(results: list[dict]) -> None:
    """Swap a short file's excerpts for its whole text (as `open` would show it), marked complete."""
    ids = [r["id"].split(":", 1)[1] for r in results if r["id"].startswith("item:")]
    if not ids:
        return
    # Overlapping chunks add up to more than the text, so this skips big documents without loading them.
    rows = await db.fetch_all(
        """select i.id, i.document from items i
           where i.id = any(%s::uuid[]) and i.document is not null
             and not exists (select 1 from datasets d where d.item_id = i.id)
             and (select coalesce(sum(length(c.text)), 0) from chunks c
                  where c.source_type = 'item' and c.source_id = i.id) <= %s""",
        (ids, FULL_TEXT_CHARS * 2),
    )
    docs = {f"item:{r['id']}": items.document_of(r) for r in rows}
    budget = FULL_TEXT_BUDGET
    for r in results:
        doc = docs.get(r["id"])
        if doc is None or doc.sheets:
            continue
        text, _ = render.document_markdown(doc)
        if not text.strip() or len(text) > min(FULL_TEXT_CHARS, budget):
            continue
        budget -= len(text)
        del r["matches"]
        r["content"], r["complete"] = text, True


def _snippet(text: str, n: int = 700) -> str:
    t = re.sub(r"\s+\n", "\n", text).strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + " …"


def where_text(locator: dict, heading: str = "") -> str:
    parts = []
    if heading:
        parts.append(heading)
    if "page" in locator:
        parts.append(f"page {locator['page']}")
    if "pages" in locator:
        ps = locator["pages"]
        parts.append(f"pages {ps[0]}–{ps[-1]}" if isinstance(ps, list) and ps else f"pages {ps}")
    if "slide" in locator:
        parts.append(f"slide {locator['slide']}")
    if "sheet" in locator:
        parts.append(f"sheet '{locator['sheet']}'" + (f" rows {locator['rows']}" if locator.get("rows") else ""))
    if "t_start" in locator:
        parts.append(f"{locator['t_start']}–{locator.get('t_end', '')}".rstrip("–"))
    elif "t" in locator:
        parts.append(str(locator["t"]))
    return " · ".join(parts)


async def _metadata(keys: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    by: dict[str, list[str]] = {}
    for t, i in keys:
        by.setdefault(t, []).append(i)
    out: dict[tuple[str, str], dict] = {}
    if by.get("item"):
        for r in await db.fetch_all(
            """select i.id, i.title, i.kind, i.url, i.path, i.status, i.source_modified_at, i.updated_at,
                      c.name as collection from items i join collections c on c.id=i.collection_id
               where i.id = any(%s::uuid[])""",
            (by["item"],),
        ):
            out[("item", str(r["id"]))] = {
                "id": f"item:{r['id']}", "type": r["kind"], "title": r["title"], "collection": r["collection"],
                "url": r["url"], "path": " / ".join(r["path"] or []), "status": r["status"],
                "date": (r["source_modified_at"] or r["updated_at"]).date().isoformat(),
            }
    if by.get("page"):
        for r in await db.fetch_all(
            """select p.id, p.title, p.kind, p.status, p.version, p.updated_at, p.review_by, c.name as collection
               from pages p join collections c on c.id=p.collection_id where p.id = any(%s::uuid[])""",
            (by["page"],),
        ):
            out[("page", str(r["id"]))] = {
                "id": f"page:{r['id']}", "type": f"{r['kind']} page", "title": r["title"],
                "collection": r["collection"], "status": r["status"], "version": r["version"],
                "date": r["updated_at"].date().isoformat(), "review_by": r["review_by"],
            }
    if by.get("record"):
        for r in await db.fetch_all(
            """select r.id, r.type, r.title, r.status, r.valid_from, r.valid_to, r.updated_at, r.superseded_by,
                      c.name as collection from records r join collections c on c.id=r.collection_id
               where r.id = any(%s::uuid[])""",
            (by["record"],),
        ):
            e = {
                "id": f"record:{r['id']}", "type": r["type"], "title": r["title"], "collection": r["collection"],
                "status": r["status"], "date": r["updated_at"].date().isoformat(),
            }
            if r["valid_from"] or r["valid_to"]:
                e["valid"] = f"{r['valid_from'] or '…'} to {r['valid_to'] or 'now'}"
            if r["superseded_by"]:
                e["superseded_by"] = f"record:{r['superseded_by']}"
            out[("record", str(r["id"]))] = e
    if by.get("dataset"):
        for r in await db.fetch_all(
            """select d.id, d.title, d.table_name, d.row_count, d.status, d.updated_at, c.name as collection
               from datasets d join collections c on c.id=d.collection_id where d.id = any(%s::uuid[])""",
            (by["dataset"],),
        ):
            out[("dataset", str(r["id"]))] = {
                "id": f"dataset:{r['id']}", "type": "dataset", "title": r["title"], "collection": r["collection"],
                "status": "active" if r["status"] == "active" else "deleted",
                "table": f"data.{r['table_name']}", "rows": r["row_count"], "date": r["updated_at"].date().isoformat(),
            }
    return out
