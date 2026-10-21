"""Re-indexing keeps the embeddings of chunks that didn't change."""

from __future__ import annotations

from helpers import drain
from nebula import access, db
from nebula.connectors.base import FetchedItem
from nebula.knowledge import items


def _doc(middle: str) -> bytes:
    sections = [
        ("Enrolment", "Centres enrol children every June. " * 30),
        ("Training", middle * 30),
        ("Reporting", "Monthly reports go to the state office by the fifth. " * 30),
    ]
    return "\n\n".join(f"# {h}\n\n{body}" for h, body in sections).encode()


async def _unembedded(item_id: str) -> int:
    row = await db.fetch_one(
        "select count(*) as n from chunks where source_type='item' and source_id=%s and embedding is null", (item_id,))
    return row["n"]


async def test_small_edit_reembeds_only_changed_chunks(mcp):
    owner = await access.ensure_person("index-owner@example.org")
    col = await db.fetch_one(
        "insert into collections (slug, name, kind, owner_person) values ('index-reuse', 'Index reuse', 'notebook', %s) "
        "returning id", (owner.id,))
    first = FetchedItem(external_id="guide", title="Programme guide", url="", mime_type="text/markdown", kind="file",
                        data=_doc("Teachers meet on Saturdays. "), filename="guide.md")
    async with db.tx() as conn:
        res = await items.upsert(conn, str(col["id"]), first)
    total = (await db.fetch_one("select count(*) as n from chunks where source_id=%s", (res["id"],)))["n"]
    assert total >= 3 and await _unembedded(res["id"]) == total
    await drain()
    assert await _unembedded(res["id"]) == 0

    edited = FetchedItem(external_id="guide", title="Programme guide", url="", mime_type="text/markdown", kind="file",
                         data=_doc("Teachers meet on Sundays. "), filename="guide.md")
    async with db.tx() as conn:
        res2 = await items.upsert(conn, str(col["id"]), edited)
    assert res2["changed"] and res2["id"] == res["id"]
    changed = await _unembedded(res["id"])
    assert 0 < changed < total  # only the Training section needs new vectors
    await drain()
    assert await _unembedded(res["id"]) == 0
