"""Files between Claude's sandbox and Nebula: download links, uploads and exports."""

from __future__ import annotations

import base64
import csv
import io
import re
import uuid

from psycopg import AsyncConnection

from nebula import access, db, jobs, limits, storage
from nebula.connectors.base import FetchedItem
from nebula.knowledge import items, links, render

INLINE_MAX = 100 * 1024


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ()]+", "_", name, flags=re.UNICODE).strip() or "file"
    return name[:120]


async def _export(key: str, data: bytes, mime: str, person_id: str) -> None:
    if await storage.exists(key) is None:
        await storage.put(key, data, mime, person_id=person_id)


async def download(person: access.Person, ref: str, inline: bool = False) -> dict:
    kind, rid = links.parse_ref(ref)
    await access.require(person, kind, rid, "view")
    if kind == "item":
        it = await items.get(rid)
        assert it is not None
        if it["file_key"]:
            key, name, mime = it["file_key"], it["filename"] or _safe_name(it["title"]), it["mime_type"]
        else:
            doc = items.document_of(it)
            md = f"# {it['title']}\n\nSource: {it['url']}\n\n" + (render.document_markdown(doc)[0] if doc else "")
            key = f"exports/{person.id}/item/{rid}/v{it['version']}.md"
            name, mime = _safe_name(it["title"]) + ".md", "text/markdown"
            await _export(key, md.encode(), mime, person.id)
    elif kind == "page":
        p = await db.fetch_one("select title, body, version from pages where id=%s", (rid,))
        assert p is not None
        key = f"exports/{person.id}/page/{rid}/v{p['version']}.md"
        name, mime = _safe_name(p["title"]) + ".md", "text/markdown"
        await _export(key, f"# {p['title']}\n\n{p['body']}".encode(), mime, person.id)
    elif kind == "record":
        r = await db.fetch_one("select type, title, body, version from records where id=%s", (rid,))
        assert r is not None
        key = f"exports/{person.id}/record/{rid}/v{r['version']}.md"
        name, mime = _safe_name(r["title"]) + ".md", "text/markdown"
        await _export(key, f"# {r['type'].capitalize()}: {r['title']}\n\n{r['body']}".encode(), mime, person.id)
    elif kind == "dataset":
        d = await db.fetch_one("select title, table_name, updated_at from datasets where id=%s", (rid,))
        assert d is not None
        key = f"exports/{person.id}/dataset/{rid}/{int(d['updated_at'].timestamp())}.csv"
        if await storage.exists(key) is None:
            rows = await db.fetch_all(f'select * from data."{d["table_name"]}"')  # noqa: S608 (name from our own table)
            buf = io.StringIO()
            if rows:
                w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            await storage.put(key, buf.getvalue().encode("utf-8-sig"), "text/csv", person_id=person.id)
        name, mime = _safe_name(d["title"]) + ".csv", "text/csv"
    else:
        raise ValueError("Downloads work for items, pages, records and datasets.")

    size = await storage.exists(key)
    if inline:
        if size is not None and size <= INLINE_MAX:
            return {"filename": name, "mime_type": mime, "base64": base64.b64encode(await storage.get(key)).decode()}
        raise ValueError(f"That file is {size} bytes; inline only works up to {INLINE_MAX} bytes. Use the link.")
    url = storage.signed_url(key, method="GET", filename=name, mime=mime, ttl=900,
                             person_id=person.id, resource_type=kind, resource_id=rid)
    return {
        "filename": name, "size_bytes": size, "url": url, "expires_in_seconds": 900,
        "how": f"curl -fsSL -o '{name}' '{url}'",
    }


async def start_upload(person: access.Person, filename: str, collection: str | None, title: str | None,
                       replaces: str | None) -> dict:
    from nebula.knowledge.notes import notebook_for

    async with db.tx() as conn:
        replaces_id = None
        if replaces:
            kind, rid = links.parse_ref(replaces)
            if kind != "item":
                raise ValueError("replaces must be an item id (item:…).")
            await access.require(person, "item", rid, "edit", conn)
            it = await db.fetch_one(
                "select i.collection_id, c.kind, c.slug from items i join collections c on c.id=i.collection_id where i.id=%s",
                (rid,), conn,
            )
            if not it or it["kind"] != "notebook":
                raise ValueError("Only files stored in Nebula notebooks can be replaced here; synced files change in "
                                 "their source (Drive/Notion). Upload this as a new file instead.")
            collection, replaces_id = it["slug"], rid
        col = await notebook_for(conn, person, collection)
        if col["kind"] != "notebook":
            raise ValueError(f"'{col['name']}' is synced from {col['kind']}; upload into a notebook collection instead.")
        uid = str(uuid.uuid4())
        name = _safe_name(filename)
        key = f"uploads/{uid}/{name}"
        await conn.execute(
            """insert into uploads (id, person_id, collection_id, filename, title, file_key, replaces_item)
               values (%s,%s,%s,%s,%s,%s,%s)""",
            (uid, person.id, col["id"], name, title or name, key, replaces_id),
        )
    url = storage.signed_url(key, method="PUT", filename=name, ttl=1800, upload_id=uid,
                             person_id=person.id, resource_type="collection", resource_id=str(col["id"]))
    return {
        "upload_id": uid, "collection": col["name"], "url": url, "expires_in_seconds": 1800,
        "how": f"curl -fsS -X PUT --data-binary @'{filename}' '{url}'",
        "next": "After the PUT succeeds Nebula indexes the file automatically; check with file action 'status'.",
    }


async def mark_received(upload_id: str, conn: AsyncConnection | None = None) -> str | None:
    """Called by the /files endpoint after a PUT lands. Queues indexing."""
    if conn is None:
        async with db.tx() as own:
            return await mark_received(upload_id, own)
    up = await db.fetch_one(
        "update uploads set status='received' where id=%s and status='pending' returning *", (upload_id,), conn
    )
    if not up:
        return None
    return await jobs.enqueue("ingest_upload", {"upload_id": upload_id}, conn=conn,
                              dedupe_key=f"upload:{upload_id}", person_id=str(up["person_id"]), priority=10)


async def status(person: access.Person, upload_id: str) -> dict:
    up = await db.fetch_one("select * from uploads where id=%s and person_id=%s", (upload_id, person.id))
    if not up:
        raise ValueError("No such upload of yours.")
    out = {"upload_id": upload_id, "status": up["status"]}
    if up["status"] == "pending":
        size = await storage.exists(up["file_key"])
        if size is not None:
            await mark_received(upload_id)
            out["status"] = "received"
        else:
            out["note"] = "Nebula hasn't received the file yet. Run the curl PUT command from the upload step."
    elif up["status"] == "failed":
        out["note"] = "Nebula couldn't index this file. Start a new upload to try again."
    if up["item_id"]:
        out["item"] = f"item:{up['item_id']}"
    return out


@jobs.handler("ingest_upload")
async def _ingest_upload(job: dict, payload: dict) -> dict:
    up = await db.fetch_one("select * from uploads where id=%s", (payload["upload_id"],))
    if not up:
        return {"skipped": "upload missing"}
    if up["status"] == "done":
        return {"item": f"item:{up['item_id']}"}
    try:
        return await _ingest(up)
    except Exception:
        if job["attempts"] >= job["max_attempts"]:
            # Out of retries: say so, and drop the staged copy rather than keep it charged forever.
            await db.execute("update uploads set status='failed' where id=%s and status='received'", (up["id"],))
            await storage.delete_temporary(up["file_key"])
        raise


async def _ingest(up: dict) -> dict:
    # The staged copy is about to become the stored file, so charge the bytes once, not twice.
    await limits.release_storage(up["file_key"])
    data = await storage.get(up["file_key"])
    external_id = f"upload:{up['id']}"
    if up["replaces_item"]:
        old = await db.fetch_one("select external_id from items where id=%s", (up["replaces_item"],))
        external_id = old["external_id"] if old else external_id
    fetched = FetchedItem(
        external_id=external_id, title=up["title"] or up["filename"], url="", mime_type=up["mime_type"],
        kind="upload", data=data, filename=up["filename"],
    )
    async with db.tx() as conn:
        res = await items.upsert(conn, str(up["collection_id"]), fetched, created_by=str(up["person_id"]))
        await conn.execute("update uploads set status='done', item_id=%s where id=%s", (res["id"], up["id"]))
    await storage.delete_temporary(up["file_key"])
    return {"item": f"item:{res['id']}", "version": res["version"]}
