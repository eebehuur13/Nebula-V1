"""Pages and records: the knowledge people save through Claude.

Pages are readable summaries (one per programme, process, metric…), with versions,
an owner, draft/verified status and a review date. Records are small typed facts
(decisions, metric definitions, lessons, corrections) with the dates they hold.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from psycopg import AsyncConnection

from nebula import access, db
from nebula.ingest.types import Chunk
from nebula.knowledge import index, links

PAGE_KINDS = ("programme", "state", "partner", "team", "process", "metric", "glossary", "person", "topic",
              "handover", "template", "other")
RECORD_TYPES = ("decision", "fact", "metric", "lesson", "correction", "action", "note")


class Conflict(Exception):
    pass


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]
    return s or "page"


# Notebooks: where saved knowledge lives --------------------------------------------------


async def notebook_for(conn: AsyncConnection, person: access.Person, collection: str | None) -> dict:
    """Resolve where to save: an explicit collection, the person's team notebook, or their own."""
    if collection:
        row = await db.fetch_one(
            "select * from collections where (slug = %s or id::text = %s) and status = 'active'",
            (collection, collection),
            conn,
        )
        if not row:
            raise ValueError(f"No collection '{collection}'.")
        lv = (await access.collection_levels(person, conn)).get(str(row["id"]), "none")
        if not access.at_least(lv, "edit"):
            raise access.AccessDenied(f"You can't add to '{row['name']}'.")
        return row
    teams = [g for g in person.groups if g != access.ALL_RL]
    if len(teams) == 1:
        return await ensure_group_notebook(conn, teams[0])
    return await ensure_personal_notebook(conn, person)


async def ensure_group_notebook(conn: AsyncConnection, group_id: str) -> dict:
    slug = f"{group_id.replace('_', '-')}-notebook"
    row = await db.fetch_one("select * from collections where slug = %s", (slug,), conn)
    if row:
        return row
    g = await db.fetch_one("select name from groups where id = %s", (group_id,), conn)
    name = f"{g['name'] if g else group_id} notebook"
    row = await db.fetch_one(
        """insert into collections (slug, name, description, kind, owner_group)
           values (%s, %s, %s, 'notebook', %s) returning *""",
        (slug, name, f"Pages and records saved by {name.removesuffix(' notebook')}", group_id),
        conn,
    )
    assert row is not None
    return row


async def ensure_personal_notebook(conn: AsyncConnection, person: access.Person) -> dict:
    # Email local parts are not unique across domains. Keep old notebooks by
    # ownership, and use the person's immutable id for every new notebook.
    slug = "me-" + person.id
    row = await db.fetch_one(
        "select * from collections where kind='notebook' and owner_person=%s and status='active' "
        "order by created_at, id limit 1", (person.id,), conn,
    )
    if row:
        return row
    row = await db.fetch_one(
        """insert into collections (slug, name, description, kind, owner_person)
           values (%s, %s, 'Private notes. Share items from here when they are ready.', 'notebook', %s)
           on conflict (slug) do update set slug=excluded.slug
           where collections.owner_person=excluded.owner_person returning *""",
        (slug, f"{person.name or person.email}'s notebook", person.id),
        conn,
    )
    assert row is not None
    return row


# Pages ---------------------------------------------------------------------------------


def _page_chunks(page: dict) -> list[Chunk]:
    from nebula.ingest import chunk_document, parse

    doc = parse(page["body"].encode(), filename="page.md", title=page["title"])
    doc.title = page["title"]
    chunks = chunk_document(doc)
    header = f"{page['title']} ({page['kind']} page, {page['status']})"
    if page.get("aliases"):
        header += " · also known as " + ", ".join(page["aliases"])
    if not chunks:
        return [Chunk(text=header, heading_path=[page["title"]])]
    chunks[0].text = header + "\n" + chunks[0].text
    return chunks


async def _reindex_page(conn: AsyncConnection, page: dict, person: access.Person) -> dict:
    await index.replace_chunks(conn, "page", str(page["id"]), str(page["collection_id"]), _page_chunks(page))
    return await links.rebuild_from_text(conn, "page", str(page["id"]), page["body"], str(page["collection_id"]), person=person)


async def create_page(conn: AsyncConnection, person: access.Person, *, collection_id: str, title: str, body: str,
                      kind: str = "topic", aliases: list[str] | None = None, status: str = "draft",
                      owner: str | None = None, review_days: int = 90, slug: str | None = None) -> dict:
    if kind not in PAGE_KINDS:
        raise ValueError(f"Page kind must be one of: {', '.join(PAGE_KINDS)}.")
    col = await db.fetch_one("select owner_group, owner_person from collections where id=%s", (collection_id,), conn)
    if owner is None and col:
        og, op = col["owner_group"], (str(col["owner_person"]) if col["owner_person"] else None)
    else:
        og, op = access.owner_fields(person, owner)
    if status == "verified":
        lv = (await access.collection_levels(person, conn)).get(collection_id, "none")
        if not access.at_least(lv, "manage"):
            status = "draft"
    base = slug or slugify(title)
    s, n = base, 2
    while await db.fetch_one("select 1 from pages where collection_id=%s and slug=%s", (collection_id, s), conn):
        s, n = f"{base}-{n}", n + 1
    page = await db.fetch_one(
        """insert into pages (collection_id, slug, title, kind, aliases, body, status, owner_group, owner_person,
                              review_by, created_by, updated_by)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
        (collection_id, s, title, kind, aliases or [], body, status, og, op,
         dt.date.today() + dt.timedelta(days=review_days), person.id, person.id),
        conn,
    )
    assert page is not None
    await conn.execute(
        "insert into page_versions (page_id, version, title, body, status, edited_by, note) values (%s,1,%s,%s,%s,%s,'created')",
        (page["id"], title, body, status, person.id),
    )
    linkinfo = await _reindex_page(conn, page, person)
    return {**page, **linkinfo}


async def update_page(conn: AsyncConnection, person: access.Person, page_id: str, *, expected_version: int | None,
                      title: str | None = None, body: str | None = None, aliases: list[str] | None = None,
                      status: str | None = None, kind: str | None = None, note: str = "") -> dict:
    page = await db.fetch_one("select * from pages where id = %s for update", (page_id,), conn)
    if not page:
        raise ValueError("No such page.")
    lv = await access.require(person, "page", page_id, "edit", conn)
    if expected_version is not None and expected_version != page["version"]:
        raise Conflict(
            f"The page changed since you read it (now version {page['version']}, you had {expected_version}). "
            "Open it again and merge your change."
        )
    can_manage = access.at_least(lv, "manage")
    if status == "verified" and not can_manage:
        raise access.AccessDenied("Only the page's owner (or a lead) can mark it verified.")
    if page["status"] == "verified" and not can_manage:
        prop = await db.fetch_one(
            """insert into proposals (page_id, base_version, title, body, note, proposed_by)
               values (%s,%s,%s,%s,%s,%s) returning id""",
            (page_id, page["version"], title, body if body is not None else page["body"], note, person.id),
            conn,
        )
        assert prop is not None
        return {**page, "proposal_id": str(prop["id"]), "proposed": True}
    new = {
        "title": title if title is not None else page["title"],
        "body": body if body is not None else page["body"],
        "aliases": aliases if aliases is not None else page["aliases"],
        "status": status or ("draft" if (body is not None and page["status"] == "verified" and not can_manage) else page["status"]),
        "kind": kind or page["kind"],
    }
    review_by = page["review_by"]
    if new["status"] == "verified" and can_manage:
        review_by = dt.date.today() + dt.timedelta(days=90)
    updated = await db.fetch_one(
        """update pages set title=%s, body=%s, aliases=%s, status=%s, kind=%s, version=version+1, updated_by=%s,
                  updated_at=now(), review_by=%s where id=%s returning *""",
        (new["title"], new["body"], new["aliases"], new["status"], new["kind"], person.id, review_by, page_id),
        conn,
    )
    assert updated is not None
    await conn.execute(
        "insert into page_versions (page_id, version, title, body, status, edited_by, note) values (%s,%s,%s,%s,%s,%s,%s)",
        (page_id, updated["version"], updated["title"], updated["body"], updated["status"], person.id, note),
    )
    linkinfo = await _reindex_page(conn, updated, person)
    return {**updated, **linkinfo}


async def decide_proposal(conn: AsyncConnection, person: access.Person, proposal_id: str, accept: bool) -> dict:
    prop = await db.fetch_one("select * from proposals where id=%s and status='open'", (proposal_id,), conn)
    if not prop:
        raise ValueError("No open proposal with that id.")
    await access.require(person, "page", str(prop["page_id"]), "manage", conn)
    await conn.execute(
        "update proposals set status=%s, decided_by=%s, decided_at=now() where id=%s",
        ("accepted" if accept else "rejected", person.id, proposal_id),
    )
    if not accept:
        return {"proposal": proposal_id, "status": "rejected"}
    page = await update_page(conn, person, str(prop["page_id"]), expected_version=None,
                             title=prop["title"], body=prop["body"], note=f"accepted proposal: {prop['note']}")
    return {"proposal": proposal_id, "status": "accepted", "page_version": page["version"]}


# Records -----------------------------------------------------------------------------


def _record_chunk(r: dict) -> Chunk:
    when = ""
    if r.get("valid_from") or r.get("valid_to"):
        when = f"\nTrue from {r.get('valid_from') or '…'} to {r.get('valid_to') or 'now'}."
    data = ""
    if r.get("data"):
        data = "\n" + "\n".join(f"{k}: {v}" for k, v in r["data"].items() if v not in (None, ""))
    text = f"{r['type'].capitalize()}: {r['title']} ({r['status']})\n{r['body']}{data}{when}"
    return Chunk(text=text.strip(), heading_path=[r["title"]], kind="text")


async def create_record(conn: AsyncConnection, person: access.Person, *, collection_id: str, type: str, title: str,
                        body: str = "", data: dict | None = None, status: str = "draft",
                        valid_from: dt.date | None = None, valid_to: dt.date | None = None,
                        supersedes: str | None = None, owner: str | None = None, source_note: str = "",
                        evidence: list[str] | None = None) -> dict:
    if type not in RECORD_TYPES:
        raise ValueError(f"Record type must be one of: {', '.join(RECORD_TYPES)}.")
    col = await db.fetch_one("select owner_group, owner_person from collections where id=%s", (collection_id,), conn)
    if owner is None and col:
        og, op = col["owner_group"], (str(col["owner_person"]) if col["owner_person"] else None)
    else:
        og, op = access.owner_fields(person, owner)
    lv = (await access.collection_levels(person, conn)).get(collection_id, "none")
    if status == "verified" and not access.at_least(lv, "manage"):
        status = "draft"
    if supersedes:
        await access.require(person, "record", supersedes, "edit", conn)
    rec = await db.fetch_one(
        """insert into records (collection_id, type, title, body, data, status, valid_from, valid_to, supersedes,
                                owner_group, owner_person, author, source_note)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
        (collection_id, type, title, body, db.jsonb(data or {}), status, valid_from, valid_to, supersedes,
         og, op, person.id, source_note),
        conn,
    )
    assert rec is not None
    rid = str(rec["id"])
    await conn.execute(
        "insert into record_versions (record_id, version, title, body, data, status, edited_by) values (%s,1,%s,%s,%s,%s,%s)",
        (rid, title, body, db.jsonb(data or {}), status, person.id),
    )
    if supersedes:
        old = await db.fetch_one(
            "update records set status='superseded', superseded_by=%s, updated_at=now(), "
            "valid_to=coalesce(valid_to, %s) where id=%s returning *",
            (rid, valid_from or dt.date.today(), supersedes),
            conn,
        )
        await links.add(conn, "record", rid, "record", supersedes, "supersedes")
        if old:
            await index.replace_chunks(conn, "record", supersedes, str(old["collection_id"]), [_record_chunk(old)])
    for ref in evidence or []:
        t, i = links.parse_ref(ref)
        await links.add(conn, "record", rid, t, i, "evidence")
    await index.replace_chunks(conn, "record", rid, collection_id, [_record_chunk(rec)])
    linkinfo = await links.rebuild_from_text(conn, "record", rid, body, collection_id, person=person)
    return {**rec, **linkinfo}


async def update_record(conn: AsyncConnection, person: access.Person, record_id: str, *, expected_version: int | None,
                        title: str | None = None, body: str | None = None, data: dict | None = None,
                        status: str | None = None, valid_from: dt.date | None = None,
                        valid_to: dt.date | None = None) -> dict:
    rec = await db.fetch_one("select * from records where id=%s for update", (record_id,), conn)
    if not rec:
        raise ValueError("No such record.")
    lv = await access.require(person, "record", record_id, "edit", conn)
    if expected_version is not None and expected_version != rec["version"]:
        raise Conflict(f"The record changed since you read it (now version {rec['version']}).")
    if status == "verified" and not access.at_least(lv, "manage"):
        raise access.AccessDenied("Only the record's owner (or a lead) can verify it.")
    new_status = status or ("draft" if rec["status"] == "verified" and not access.at_least(lv, "manage") else rec["status"])
    upd = await db.fetch_one(
        """update records set title=%s, body=%s, data=%s, status=%s, valid_from=%s, valid_to=%s,
                  version=version+1, updated_at=now() where id=%s returning *""",
        (title if title is not None else rec["title"], body if body is not None else rec["body"],
         db.jsonb(data if data is not None else rec["data"]), new_status,
         valid_from if valid_from is not None else rec["valid_from"],
         valid_to if valid_to is not None else rec["valid_to"], record_id),
        conn,
    )
    assert upd is not None
    await conn.execute(
        "insert into record_versions (record_id, version, title, body, data, status, edited_by) values (%s,%s,%s,%s,%s,%s,%s)",
        (record_id, upd["version"], upd["title"], upd["body"], db.jsonb(upd["data"]), upd["status"], person.id),
    )
    await index.replace_chunks(conn, "record", record_id, str(upd["collection_id"]), [_record_chunk(upd)])
    linkinfo = await links.rebuild_from_text(conn, "record", record_id, upd["body"], str(upd["collection_id"]), person=person)
    return {**upd, **linkinfo}


def iso(d: Any) -> str | None:
    return d.isoformat() if isinstance(d, (dt.date, dt.datetime)) else d
