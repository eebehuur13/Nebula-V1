"""Synced sources keep only their current copy; a version Nebula can't read never replaces a good one."""

from __future__ import annotations

import io

from helpers import drain
from nebula import access, db, storage
from nebula.connectors.base import FetchedItem
from nebula.knowledge import items


def _docx(text: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _file(data: bytes, name: str = "report.docx") -> FetchedItem:
    return FetchedItem(external_id="kept-file", title="Field report", url="", mime_type="", kind="doc",
                       data=data, filename=name)


async def _collection(slug: str, kind: str, owner_id: str) -> str:
    row = await db.fetch_one(
        "insert into collections (slug, name, kind, source_config, owner_person) values (%s, %s, %s, %s, %s) returning id",
        (slug, slug, kind, db.jsonb({"folder_id": f"{slug}-root"} if kind == "gdrive" else {}), owner_id))
    return str(row["id"])


async def _keys(item_id: str) -> list[str | None]:
    rows = await db.fetch_all("select file_key from item_versions where item_id=%s order by version", (item_id,))
    return [r["file_key"] for r in rows]


async def test_synced_files_keep_only_the_current_copy(mcp):
    owner = await access.ensure_person("storage-owner@example.org")
    cid = await _collection("storage-synced", "gdrive", owner.id)
    async with db.tx() as conn:
        first = await items.upsert(conn, cid, _file(_docx("Visited 12 centres in Guntur.")))
    async with db.tx() as conn:
        second = await items.upsert(conn, cid, _file(_docx("Visited 14 centres in Guntur and Krishna.")))
    assert second["version"] == 2
    keys = await _keys(first["id"])
    assert keys[0] is None and keys[1]  # the old copy is released...
    await drain()
    old_key = f"items/{cid}/{first['id']}/v1/report.docx"
    assert await storage.exists(old_key) is None  # ...and deleted once the change committed
    charged = await db.fetch_all("select file_key from stored_objects where person_id=%s", (owner.id,))
    assert [r["file_key"] for r in charged] == [keys[1]]

    # A newer version Nebula can't read doesn't replace the readable one.
    async with db.tx() as conn:
        res = await items.upsert(conn, cid, _file(b"PK\x03\x04 not really a zip"))
    assert res["kept_previous"] and res["version"] == 2
    it = await items.get(first["id"])
    assert "couldn't read" in it["status_note"] and it["file_key"] == keys[1]
    assert "14 centres" in items.document_of(it).blocks[0].text

    # Deleted in the source: the stored copy goes too; the row stays as a marker.
    async with db.tx() as conn:
        await items.mark_deleted(conn, cid, "kept-file")
    await drain()
    it = await items.get(first["id"])
    assert it["status"] == "deleted" and it["file_key"] is None and it["document"] is None
    assert await storage.exists(keys[1]) is None
    assert not await db.fetch_all("select 1 from stored_objects where person_id=%s", (owner.id,))


async def test_uploads_keep_their_versions(mcp):
    owner = await access.ensure_person("storage-uploader@example.org")
    cid = await _collection("storage-notebook", "notebook", owner.id)
    async with db.tx() as conn:
        first = await items.upsert(conn, cid, _file(_docx("Draft one.")), created_by=owner.id)
    async with db.tx() as conn:
        await items.upsert(conn, cid, _file(_docx("Draft two.")), created_by=owner.id)
    await drain()
    keys = await _keys(first["id"])
    assert all(keys) and all([await storage.exists(k) for k in keys])
