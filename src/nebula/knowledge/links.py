"""The graph: links between pages, records, sources and datasets.

Page and record bodies use two kinds of reference, and both become links:
- `[[slug]]` or `[[Page title]]` links to another page (rel links_to)
- `nebula:item:<uuid>` (optionally `#page=3`) cites a source, record, page or dataset (rel cites)
Evidence passed explicitly when saving becomes rel evidence.
"""

from __future__ import annotations

import re

from psycopg import AsyncConnection

from nebula import access, db

WIKI = re.compile(r"\[\[([^\[\]|#]{1,120})(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
REF = re.compile(r"nebula:(item|page|record|dataset):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:#([\w=.,:-]+))?")


def parse_ref(ref: str) -> tuple[str, str]:
    """'item:<uuid>' / 'nebula:item:<uuid>' -> ('item', '<uuid>')."""
    ref = ref.strip().removeprefix("nebula:")
    if ":" not in ref:
        raise ValueError(f"'{ref}' isn't a Nebula id. Ids look like 'page:1b2c…' (as returned by search).")
    kind, _, rid = ref.partition(":")
    rid = rid.split("#", 1)[0]
    if kind not in ("item", "page", "record", "dataset", "skill", "collection"):
        raise ValueError(f"Unknown kind '{kind}' in id '{ref}'.")
    return kind, rid


async def resolve_page(conn: AsyncConnection, target: str, prefer_collection: str | None,
                       *, person: access.Person) -> str | None:
    t = target.strip()
    slug = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    rows = await db.fetch_all(
        """select id, collection_id from pages
           where status <> 'archived' and (slug = %s or lower(title) = lower(%s) or %s = any(aliases))
           order by (collection_id = %s) desc nulls last limit 40""",
        (slug, t, t, prefer_collection),
        conn,
    )
    for row in rows:
        if access.at_least(await access.level_for(person, "page", str(row["id"]), conn), "view"):
            return str(row["id"])
    return None


async def rebuild_from_text(
    conn: AsyncConnection, from_type: str, from_id: str, text: str, collection_id: str | None,
    *, person: access.Person,
) -> dict:
    """Replace the links_to/cites links a page or record makes in its text."""
    await conn.execute(
        "delete from links where from_type=%s and from_id=%s and rel in ('links_to','cites')", (from_type, from_id)
    )
    unresolved: list[str] = []
    n = 0
    for m in WIKI.finditer(text):
        pid = await resolve_page(conn, m.group(1), collection_id, person=person)
        if pid and pid != from_id:
            await add(conn, from_type, from_id, "page", pid, "links_to")
            n += 1
        elif not pid:
            unresolved.append(m.group(1))
    for m in REF.finditer(text):
        anchor = dict(kv.split("=", 1) for kv in (m.group(3) or "").split(",") if "=" in kv)
        await add(conn, from_type, from_id, m.group(1), m.group(2), "cites", anchor)
        n += 1
    return {"links": n, "unresolved": sorted(set(unresolved))}


async def add(conn: AsyncConnection, from_type: str, from_id: str, to_type: str, to_id: str, rel: str,
              anchor: dict | None = None) -> None:
    await conn.execute(
        """insert into links (from_type, from_id, to_type, to_id, rel, anchor) values (%s,%s,%s,%s,%s,%s)
           on conflict (from_type, from_id, to_type, to_id, rel) do update set anchor = excluded.anchor""",
        (from_type, from_id, to_type, to_id, rel, db.jsonb(anchor or {})),
    )


async def neighbours(conn: AsyncConnection | None, rtype: str, rid: str, limit: int = 40,
                     *, person: access.Person) -> dict:
    """Outgoing and incoming links with titles, for `open` and the graph view."""
    out = await db.fetch_all(
        f"""select l.to_type as type, l.to_id as id, l.rel, l.anchor, {_TITLE_SQL('l.to_type', 'l.to_id')} as title
            from links l where l.from_type=%s and l.from_id=%s limit %s""",
        (rtype, rid, limit),
        conn,
    )
    inc = await db.fetch_all(
        f"""select l.from_type as type, l.from_id as id, l.rel, {_TITLE_SQL('l.from_type', 'l.from_id')} as title
            from links l where l.to_type=%s and l.to_id=%s limit %s""",
        (rtype, rid, limit),
        conn,
    )
    visible = {}
    for direction, rows in (("outgoing", out), ("incoming", inc)):
        visible[direction] = [r for r in rows if access.at_least(
            await access.level_for(person, r["type"], str(r["id"]), conn), "view")]
    return visible


def _TITLE_SQL(t: str, i: str) -> str:  # noqa: N802
    return f"""case {t}
        when 'page' then (select title from pages where id = {i})
        when 'record' then (select title from records where id = {i})
        when 'item' then (select title from items where id = {i})
        when 'dataset' then (select title from datasets where id = {i})
        end"""
