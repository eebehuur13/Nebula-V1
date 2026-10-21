"""Sources: store the original, a structured Document, chunks and any data tables."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import replace
from typing import Any

from psycopg import AsyncConnection

from nebula import db, jobs, storage
from nebula.connectors.base import FetchedItem
from nebula.ingest.types import Chunk, Document
from nebula.knowledge import datasets, index

log = logging.getLogger(__name__)

PREVIEW_ROWS = 50  # rows of each sheet kept in items.document; the rest live in SQL tables


def _summary(doc: Document | None) -> str:
    if doc is None:
        return ""
    for b in doc.blocks:
        if b.kind in ("paragraph", "list", "slide") and len(b.text.strip()) > 40:
            return b.text.strip()[:400]
    return ""


def _doc_for_storage(doc: Document) -> dict:
    slim = replace(doc, sheets=[replace(s, rows=s.rows[:PREVIEW_ROWS]) for s in doc.sheets])
    return json.loads(json.dumps(slim.to_dict(), default=str))


def _unreadable(doc: Document | None, note: str) -> bool:
    """The parser failed on this file (as opposed to it being a type Nebula only stores)."""
    if doc is None:
        return note.startswith("could not read")
    return bool(doc.metadata.get("parse_error") or doc.metadata.get("encrypted"))


def _parse(data: bytes, filename: str, mime: str, title: str) -> tuple[Document | None, str]:
    from nebula.ingest import UnsupportedFormat, parse

    try:
        return parse(data, filename=filename, mime=mime or None, title=title), ""
    except UnsupportedFormat as e:
        return None, f"stored as a file only: {e}"
    except Exception as e:  # a broken file must not stop a sync
        log.warning("parse failed for %s: %s", filename, e)
        return None, f"could not read this file: {e}"


async def upsert(
    conn: AsyncConnection,
    collection_id: str,
    fetched: FetchedItem,
    *,
    created_by: str | None = None,
    force: bool = False,
    charge_to: str | None = None,
) -> dict:
    """Create or update one item from a connector or an upload. Returns {id, changed, version}.

    The stored original counts against `charge_to` (for Drive, whoever shared the folder or file
    with Nebula), else the uploader, else the collection's owner.
    """
    written: list[str] = []
    try:
        return await _upsert(conn, collection_id, fetched, created_by, force, written, charge_to)
    except Exception:
        # The transaction rolls back, so a file stored for a new item would stay charged with nothing using it.
        for key in written:
            await storage.discard_unreferenced(key)
        raise


async def _upsert(conn: AsyncConnection, collection_id: str, fetched: FetchedItem, created_by: str | None,
                  force: bool, written: list[str], charge_to: str | None = None) -> dict:
    if fetched.data is not None:
        h = storage.content_hash(fetched.data)
    elif fetched.document is not None:
        h = hashlib.sha256(json.dumps(fetched.document.to_dict(), sort_keys=True, default=str).encode()).hexdigest()
    else:
        h = None

    col = await db.fetch_one("select kind, coalesce(owner_person, created_by) as person from collections where id=%s",
                             (collection_id,), conn)
    assert col is not None
    synced = col["kind"] != "notebook"
    existing = await db.fetch_one(
        """select id, content_hash, version, status,
                  (document is not null and document->'metadata'->>'parse_error' is null) as readable
           from items where collection_id = %s and external_id = %s""",
        (collection_id, fetched.external_id),
        conn,
    )
    meta = dict(
        title=fetched.title, url=fetched.url, mime_type=fetched.mime_type, filename=fetched.filename,
        path=fetched.path, author=fetched.author, kind=fetched.kind,
        source_created_at=fetched.created_at, source_modified_at=fetched.modified_at,
    )
    if existing and existing["content_hash"] == h and existing["status"] == "active" and not force:
        await conn.execute(
            """update items set title=%(title)s, url=%(url)s, path=%(path)s, author=%(author)s,
                   source_modified_at=%(source_modified_at)s, updated_at=now() where id=%(id)s""",
            {**meta, "id": existing["id"]},
        )
        return {"id": str(existing["id"]), "changed": False, "version": existing["version"]}

    doc = fetched.document
    note = ""
    name = fetched.filename or "file"
    if fetched.data is not None and doc is None:
        doc, note = _parse(fetched.data, name, fetched.mime_type, fetched.title)
    if synced and existing and existing["status"] == "active" and existing["readable"] and _unreadable(doc, note):
        # Never swap a readable copy for one Nebula couldn't read: keep the last good version, and say so.
        reason = (note or (doc.metadata.get("parse_error") if doc else "") or "unreadable")[:200]
        when = fetched.modified_at.date().isoformat() if fetched.modified_at else "recently"
        await conn.execute(
            "update items set content_hash=%s, status_note=%s where id=%s",
            (h, f"The source has a newer version ({when}) that Nebula couldn't read ({reason}). This is the last "
                "version it could read.", existing["id"]))
        return {"id": str(existing["id"]), "changed": False, "version": existing["version"], "kept_previous": True}

    item_id = str(existing["id"]) if existing else str(uuid.uuid4())
    version = (existing["version"] if existing else 0) + 1

    file_key = None
    if fetched.data is not None:
        file_key = f"items/{collection_id}/{item_id}/v{version}/{name}"
        owner = charge_to or created_by or (str(col["person"]) if col["person"] else None)
        await storage.put(file_key, fetched.data, fetched.mime_type, person_id=owner)
        if not existing:
            # A new item's key is unique to this attempt. An existing item's key is reused by the retry.
            written.append(file_key)
    if doc is not None and not doc.title:
        doc.title = fetched.title
    if doc is not None:
        from nebula.ingest.quality import assess

        doc.metadata["extraction"] = assess(doc)

    await conn.execute(
        """
        insert into items (id, collection_id, external_id, kind, title, url, mime_type, filename, path, author,
                           source_created_at, source_modified_at, content_hash, version, file_key, document,
                           summary, status, status_note, created_by, indexed_at)
        values (%(id)s, %(collection_id)s, %(external_id)s, %(kind)s, %(title)s, %(url)s, %(mime_type)s,
                %(filename)s, %(path)s, %(author)s, %(source_created_at)s, %(source_modified_at)s, %(hash)s,
                %(version)s, %(file_key)s, %(document)s, %(summary)s, 'active', %(note)s, %(created_by)s, now())
        on conflict (collection_id, external_id) do update set
            kind=excluded.kind, title=excluded.title, url=excluded.url, mime_type=excluded.mime_type,
            filename=excluded.filename, path=excluded.path, author=excluded.author,
            source_created_at=excluded.source_created_at, source_modified_at=excluded.source_modified_at,
            content_hash=excluded.content_hash, version=excluded.version,
            file_key=coalesce(excluded.file_key, items.file_key), document=excluded.document,
            summary=excluded.summary, status='active', status_note=excluded.status_note,
            indexed_at=now(), updated_at=now()
        """,
        {
            **meta, "id": item_id, "collection_id": collection_id, "external_id": fetched.external_id,
            "hash": h, "version": version, "file_key": file_key,
            "document": db.jsonb(_doc_for_storage(doc)) if doc else None,
            "summary": _summary(doc), "note": note, "created_by": created_by,
        },
    )
    await conn.execute(
        """insert into item_versions (item_id, version, content_hash, file_key, title, source_modified_at)
           values (%s, %s, %s, %s, %s, %s) on conflict do nothing""",
        (item_id, version, h, file_key, fetched.title, fetched.modified_at),
    )
    if synced and file_key:
        # Synced sources keep only the current original: the source system has the history. Older
        # copies go once this transaction commits (the job is queued inside it).
        old = await db.fetch_all(
            """with old as (select version, file_key from item_versions
                            where item_id=%(id)s and version < %(v)s and file_key is not null for update)
               update item_versions v set file_key = null from old
               where v.item_id=%(id)s and v.version = old.version returning old.file_key""",
            {"id": item_id, "v": version}, conn)
        for r in old:
            if r["file_key"] != file_key:
                await jobs.enqueue("discard_file", {"key": r["file_key"]}, conn=conn, priority=150, max_attempts=3)
    await reindex(conn, item_id, collection_id, doc, fetched.title)
    return {"id": item_id, "changed": True, "version": version}


async def reindex(conn: AsyncConnection, item_id: str, collection_id: str, doc: Document | None, title: str) -> None:
    from nebula.ingest import chunk_document

    chunks: list[Chunk] = []
    keep: set[str] = set()
    if doc is not None:
        chunks = [c for c in chunk_document(doc) if not (c.kind == "card" and _is_data_sheet(doc, c))]
        for sheet in doc.sheets:
            if sheet.kind == "data" and sheet.rows:
                _, card = await datasets.load_sheet(
                    conn, collection_id=collection_id, item_id=item_id, doc_title=title or doc.title, sheet=sheet
                )
                keep.add(sheet.name)
                ds = await db.fetch_one(
                    "select id from datasets where item_id=%s and sheet=%s", (item_id, sheet.name), conn
                )
                assert ds is not None
                await index.replace_chunks(conn, "dataset", str(ds["id"]), collection_id, [card])
    else:
        chunks = [Chunk(text=f"{title}\n(file without readable text)", heading_path=[title])]
    await datasets.drop_for_item(conn, item_id, keep_sheets=keep)
    chunks += await _transcription_chunks(conn, item_id, title or (doc.title if doc else ""))
    await index.replace_chunks(conn, "item", item_id, collection_id, chunks)


def part_label(part: str) -> str:
    kind, _, value = part.partition(":")
    return "the whole file" if part == "all" else f"{kind} {value}".strip()


def part_locator(part: str) -> dict:
    kind, _, value = part.partition(":")
    if kind in ("page", "slide", "image") and value.isdigit():
        return {kind: int(value)}
    return {"sheet": value} if kind == "sheet" else {}


async def transcriptions(conn: AsyncConnection | None, item_id: str) -> list[dict]:
    """What Claude read from this version's unreadable parts, oldest first."""
    return await db.fetch_all(
        """select e.part, e.text, e.created_at, p.email as added_by from item_extractions e
           join items i on i.id = e.item_id and i.version = e.version
           left join people p on p.id = e.created_by
           where e.item_id = %s order by e.created_at""", (item_id,), conn)


async def _transcription_chunks(conn: AsyncConnection, item_id: str, title: str) -> list[Chunk]:
    from nebula.ingest.chunk import split_prose

    out = []
    for t in await transcriptions(conn, item_id):
        label = part_label(t["part"])
        for piece in split_prose(t["text"], 1800, 150):
            out.append(Chunk(text=piece, heading_path=[title, f"{label} (read from the image)"],
                             locator={**part_locator(t["part"]), "transcribed": True}))
    return out


def _is_data_sheet(doc: Document, chunk: Chunk) -> bool:
    name = chunk.locator.get("sheet")
    return any(s.name == name and s.kind == "data" and s.rows for s in doc.sheets)


async def mark_deleted(conn: AsyncConnection, collection_id: str, external_id: str) -> str | None:
    row = await db.fetch_one(
        "update items set status='deleted', updated_at=now() where collection_id=%s and external_id=%s "
        "and status <> 'deleted' returning id",
        (collection_id, external_id),
        conn,
    )
    if not row:
        return None
    item_id = str(row["id"])
    await index.drop_chunks(conn, "item", item_id)
    await datasets.drop_for_item(conn, item_id)
    await purge_files(conn, [item_id])
    return item_id


async def purge_files(conn: AsyncConnection, item_ids: list[str]) -> int:
    """Drop the stored copies of synced items that left their source (the source keeps its own).

    Uploads to notebooks are Nebula's own files and are never purged here. The item rows stay as
    markers, so a file that comes back is simply fetched again.
    """
    if not item_ids:
        return 0
    synced = await db.fetch_all(
        """select i.id from items i join collections c on c.id = i.collection_id
           where i.id = any(%s::uuid[]) and c.kind <> 'notebook'""", (item_ids,), conn)
    ids = [str(r["id"]) for r in synced]
    if not ids:
        return 0
    keys = await db.fetch_all(
        """select file_key from items where id = any(%(ids)s::uuid[]) and file_key is not null
           union select file_key from item_versions where item_id = any(%(ids)s::uuid[]) and file_key is not null""",
        {"ids": ids}, conn)
    await conn.execute("update items set file_key = null, document = null where id = any(%s::uuid[])", (ids,))
    await conn.execute("update item_versions set file_key = null where item_id = any(%s::uuid[])", (ids,))
    for r in keys:
        await jobs.enqueue("discard_file", {"key": r["file_key"]}, conn=conn, priority=150, max_attempts=3)
    return len(keys)


@jobs.handler("discard_file")
async def _discard_file(job: dict, payload: dict) -> dict:
    await storage.discard_unreferenced(payload["key"])
    return {"discarded": payload["key"]}


async def get(item_id: str, conn: AsyncConnection | None = None) -> dict | None:
    return await db.fetch_one(
        """select i.*, c.name as collection_name, c.slug as collection_slug, c.kind as collection_kind
           from items i join collections c on c.id = i.collection_id where i.id = %s""",
        (item_id,),
        conn,
    )


def document_of(row: dict[str, Any]) -> Document | None:
    return Document.from_dict(row["document"]) if row.get("document") else None


async def apply_source_audience(conn: AsyncConnection, collection: dict, item_id: str, audience: dict | None) -> dict:
    """Mirror the source's sharing onto the item (and its datasets) as view grants.

    Previous source grants are replaced, so a share removed in Drive is removed here on the
    next sync. Grants people made in Nebula are never touched. People named by email get an
    account row so the grant applies the moment they sign in with that Google account.
    Google Groups map to Nebula groups with the same google_group_email; the organisation's
    own domain maps to All RL. Public links and other domains are recorded but not mirrored.
    """
    from nebula.config import settings

    if (collection.get("source_config") or {}).get("permissions", "mirror") != "mirror":
        return {"mirrored": False}
    targets = [("item", item_id)] + [
        ("dataset", str(d["id"]))
        for d in await db.fetch_all("select id from datasets where item_id=%s", (item_id,), conn)
    ]
    for rtype, rid in targets:
        await conn.execute("delete from grants where resource_type=%s and resource_id=%s and origin='source'",
                           (rtype, rid))
    await conn.execute("update items set source_audience=%s where id=%s", (db.jsonb(audience or {}), item_id))
    if not audience or audience.get("unknown"):
        return {"mirrored": False, "reason": "source permissions unknown"}

    principals: list[tuple[str, str]] = []
    for email in audience.get("emails", []):
        row = await db.fetch_one(
            "insert into people (email) values (%s) on conflict (email) do update set email=excluded.email "
            "returning id, status", (email.lower(),), conn)
        if row and row["status"] == "active":
            principals.append(("person", str(row["id"])))
    unmapped_groups = []
    for gmail in audience.get("groups", []):
        g = await db.fetch_one("select id from groups where lower(google_group_email)=lower(%s)", (gmail,), conn)
        if g:
            principals.append(("group", g["id"]))
        else:
            unmapped_groups.append(gmail)
    if set(audience.get("domains", [])) & settings().allowed_domains:
        principals.append(("group", "all_rl"))
    for rtype, rid in targets:
        for ptype, pid in principals:
            await conn.execute(
                """insert into grants (resource_type, resource_id, principal_type, principal_id, level, origin)
                   values (%s, %s, %s, %s, 'view', 'source') on conflict do nothing""",
                (rtype, rid, ptype, pid),
            )
    return {"mirrored": True, "principals": len(principals), "unmapped_groups": unmapped_groups,
            "public_link_ignored": bool(audience.get("public"))}


FIX_HINT = ("Text extraction missed these parts. If they matter for what the person asked, open each one with "
            "`part` and as_image=true, read it, and save what it says with save kind='transcription' (id, part, "
            "body, expected_version). Transcribe faithfully, including table values; don't summarise or guess at "
            "what you can't read. 'uncertain' parts may be fine (a logo or a decorative photo).")


def extraction_summary(doc: Document | None, done: list[dict]) -> dict | None:
    """What text extraction may have missed in this version, minus the parts already transcribed."""
    ext = (doc.metadata.get("extraction") if doc else None) or {}
    if not ext or ext.get("status") == "ok":
        return None
    finished = {t["part"] for t in done}
    still = [g for g in ext.get("gaps", []) if g["part"] not in finished]
    out: dict[str, Any] = {"status": ext["status"] if still else "transcribed", "parts_not_read": still[:15],
                           "parts_transcribed": sorted(finished)}
    if len(still) > 15 or ext.get("total_gaps", 0) > len(ext.get("gaps", [])):
        out["more_parts_not_read"] = max(len(still), ext.get("total_gaps", 0) - len(finished)) - 15
    if still:
        out["how_to_fix"] = FIX_HINT
    return out


def _part_exists(doc: Document | None, part: str) -> bool:
    if part == "all":
        return True
    kind, _, value = part.partition(":")
    m = doc.metadata if doc else {}
    if kind in ("page", "slide", "image") and value.isdigit() and int(value) >= 1:
        n = int(value)
        limit = {"page": m.get("page_count"), "slide": m.get("slide_count"),
                 "image": (m.get("images") or {}).get("count")}[kind]
        return limit is None or n <= int(limit)
    if kind == "sheet":
        return any(s.name.lower() == value.lower() for s in (doc.sheets if doc else []))
    return False


async def save_transcription(conn: AsyncConnection, person: Any, item_id: str, part: str, text: str,
                             expected_version: int | None) -> dict:
    """Store what Claude read from a part extraction missed, for this version, and index it."""
    from nebula import access

    await access.require(person, "item", item_id, "view", conn)
    it = await db.fetch_one("select * from items where id=%s for update", (item_id,), conn)
    if not it or it["status"] != "active":
        raise ValueError("That file isn't in Nebula any more.")
    if expected_version != it["version"]:
        raise ValueError(f"The file changed since you read it (now version {it['version']}). Open it again and "
                         "read the new version before saving what a part says.")
    part = part.strip().lower() if not part.strip().lower().startswith("sheet:") else part.strip()
    doc = document_of(it)
    if not _part_exists(doc, part):
        raise ValueError(f"'{part}' isn't a part of this file. Use a part from its extraction list, e.g. 'page:3', "
                         "'slide:5', 'image:2', 'sheet:Name' or 'all'.")
    text = (text or "").strip()
    if not text:
        raise ValueError("Give what the part says in `body`.")
    if len(text) > 20_000:
        raise ValueError("That's over 20,000 characters for one part; save it in smaller parts.")
    await conn.execute(
        """insert into item_extractions (item_id, version, part, text, created_by) values (%s, %s, %s, %s, %s)
           on conflict (item_id, version, part) do update set text=excluded.text, created_by=excluded.created_by,
                                                              created_at=now()""",
        (item_id, it["version"], part, text, person.id))
    await reindex(conn, item_id, str(it["collection_id"]), doc, it["title"])
    summary = extraction_summary(doc, await transcriptions(conn, item_id))
    return {"saved": True, "id": f"item:{item_id}", "part": part, "version": it["version"],
            "parts_still_not_read": [g["part"] for g in (summary or {}).get("parts_not_read", [])],
            "note": "Saved and indexed: anyone who can open this file will find it in search."}
