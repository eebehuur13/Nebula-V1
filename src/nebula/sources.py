"""Sources people bring in themselves: Google Drive folders, single files and Shared Drives.

Sharing something with Nebula's service account in Drive is all it takes. Discovery checks every
couple of minutes for newly shared folders, files and Shared Drives, registers each one as a root of
the single Drive collection and starts importing it. Asking Claude to connect or sync a link does the
same straight away, and refreshes one file on request ("I've updated this doc, use the new version").

Whoever shared a root manages it (Drive tells us who: `sharingUser`), and its stored files count
against their storage. Access never comes from the root: each file is visible only to the people
Drive shares it with.
"""

from __future__ import annotations

import logging
import uuid

from psycopg import AsyncConnection

from nebula import access, db, jobs, org, sync
from nebula.access import AccessDenied, Person
from nebula.knowledge import index, items, links

log = logging.getLogger(__name__)

DRIVE_SLUG = "drive"
SHARE_AS = ("as an Editor. Nebula only ever reads (its Google access is read-only), but Drive only shows who "
            "a file is shared with to editors, and Nebula needs that to show each file to the right people")
VIEWER_NOTE = ("Shared with Nebula as a Viewer, so Nebula can read the files but not who they're shared with: "
               "they stay hidden from everyone but admins. In Drive, change Nebula's access to Editor (it still "
               "only reads), and the files become visible to the right people within minutes.")
ACCESS_NOTE = ("Each file is visible in Nebula only to the people Drive shares it with (people, Google Groups "
               "mapped to Nebula teams, and the RL domain), and follows Drive when sharing changes.")


def _bot() -> str:
    from nebula.connectors import gdrive

    return gdrive.service_account_email()


async def drive_space(conn: AsyncConnection | None = None) -> dict:
    """The one collection everything shared with Nebula in Drive lands in."""
    sql = "select * from collections where slug=%s and kind='gdrive'"
    row = await db.fetch_one(sql, (DRIVE_SLUG,), conn)
    if row is None:
        await db.execute(
            """insert into collections (slug, name, description, kind, source_config)
               values (%s, 'Google Drive', 'Folders, files and Shared Drives people share with Nebula.', 'gdrive',
                       '{"permissions": "mirror", "space": true}') on conflict (slug) do nothing""",
            (DRIVE_SLUG,), conn)
        row = await db.fetch_one(sql, (DRIVE_SLUG,), conn)
    if row is None or not sync.is_space(row):
        raise ValueError("The Drive source collection is missing. Ask an administrator.")
    return row


async def _person_for(conn: AsyncConnection, email: str) -> str | None:
    if not email:
        return None
    row = await db.fetch_one(
        "insert into people (email) values (%s) on conflict (email) do update set email=excluded.email "
        "returning id, status", (email.lower(),), conn)
    return str(row["id"]) if row and row["status"] == "active" else None


# Discovery ----------------------------------------------------------------------------------


@jobs.handler("drive_discover")
async def discover(job: dict | None = None, payload: dict | None = None) -> dict:
    """Register everything newly shared with Nebula in Drive, and revive roots shared again."""
    from nebula.connectors import get_connector

    if not _bot():
        return {"skipped": "Drive isn't configured"}
    space = await drive_space()
    connector = get_connector("gdrive", {"roots": []})
    try:
        found = await connector.shared_with_nebula()  # type: ignore[attr-defined]
    finally:
        await connector.close()
    known = {r["external_id"]: r for r in await db.fetch_all(
        "select id, external_id, status, status_note from source_roots where collection_id=%s", (space["id"],))}
    added, revived, upgraded = [], [], []
    async with db.tx() as conn:
        for f in found:
            row = known.get(f["id"])
            note = VIEWER_NOTE if f.get("editor") is False else ""
            if row is None:
                manager = await _person_for(conn, f["shared_by"])
                await conn.execute(
                    """insert into source_roots (collection_id, external_id, kind, name, managed_by, shared_by,
                                                 added_via, status_note)
                       values (%s, %s, %s, %s, %s, %s, 'discovery', %s) on conflict do nothing""",
                    (space["id"], f["id"], f["kind"], f["name"], manager, f["shared_by"], note))
                added.append(f["name"] or f["id"])
            elif row["status"] == "lost":
                await conn.execute(
                    """update source_roots set status='active', status_note=%s, name=%s, updated_at=now()
                       where id=%s""", (note, f["name"], row["id"]))
                revived.append(f["name"] or f["id"])
            elif row["status"] == "active" and note != (row["status_note"] or "") and \
                    (note or row["status_note"] == VIEWER_NOTE):
                await conn.execute("update source_roots set status_note=%s, updated_at=now() where id=%s",
                                   (note, row["id"]))
                if not note:
                    upgraded.append(f["id"])  # now an Editor: re-read the sharing of everything under it
            # A 'removed' root was disconnected on purpose; only an explicit connect brings it back.
        if added or revived:
            await jobs.enqueue("sync", {"collection_id": str(space["id"])}, conn=conn,
                               dedupe_key=f"sync-roots:{space['id']}", priority=30)
        if upgraded:
            await jobs.enqueue("sync", {"collection_id": str(space["id"]), "rewalk": upgraded}, conn=conn,
                               dedupe_key=f"sync-upgraded:{space['id']}", priority=30)
        if added or revived or upgraded:
            await org.audit(conn, None, "drive_discover", "collection", str(space["id"]),
                            {"added": added, "revived": revived, "now_editor": upgraded})
    return {"shared_with_nebula": len(found), "added": added, "revived": revived, "now_editor": upgraded}


# Queueing work -------------------------------------------------------------------------------


async def _queue_file(col: dict, external_id: str, person: Person) -> str | None:
    """Fetch one Drive file now (even if Drive says it hasn't changed) and index it straight away."""
    return await jobs.enqueue("sync_item", {"collection_id": str(col["id"]), "external_id": external_id,
                                            "refetch": True},
                              dedupe_key=f"sync-item:{col['id']}:{external_id}", person_id=person.id, priority=5)


async def _queue_root(col: dict, root: dict, person: Person, *, rewalk: bool = False) -> str | None:
    payload: dict = {"collection_id": str(col["id"])}
    if rewalk:
        payload["rewalk"] = [root["external_id"]]
    return await jobs.enqueue("sync", payload, dedupe_key=f"sync-root:{root['id']}", person_id=person.id, priority=20)


async def _with_eta(out: dict, job: str | None) -> dict:
    out["job"] = job
    out.update(await jobs.eta(job))
    out["next"] = ("It's ready once the job is done: check with the job tool, or sources action 'status'. "
                   "Tell the person roughly how long, and don't treat it as updated before then.")
    return out


@jobs.handler("sync_item")
async def sync_item(job: dict, payload: dict) -> dict:
    """Bring one Drive file up to date and make it searchable before the job finishes."""
    col = await db.fetch_one("select * from collections where id=%s", (payload["collection_id"],))
    if not col or col["status"] != "active" or col["kind"] != "gdrive":
        return {"skipped": True}
    ext = payload["external_id"]
    roots = await sync.active_roots(col) if sync.is_space(col) else []
    connector = sync.connector_for(col, roots)
    try:
        change = await connector.change_for(ext, col["sync_cursor"])  # type: ignore[attr-defined]
        if change is None:
            async with db.tx() as conn:
                gone = await items.mark_deleted(conn, str(col["id"]), ext)
            return {"available": False, "removed_from_nebula": bool(gone),
                    "note": "Nebula can't read that file in Drive any more, or it's no longer in anything shared "
                            "with Nebula."}
        outcome, item_id = await sync.process_change(col, connector, change, refetch=bool(payload.get("refetch")),
                                                     charge_to=sync.managers_of(roots).get(change.root or ""))
    finally:
        await connector.close()
    if not item_id:
        return {"available": False, "outcome": outcome,
                "note": "Nebula couldn't download or read that file (it may be too large, or downloads are "
                        "restricted for viewers)."}
    await index.embed_pending(source=("item", item_id))
    for d in await db.fetch_all("select id from datasets where item_id=%s", (item_id,)):
        await index.embed_pending(source=("dataset", str(d["id"])))
    it = await db.fetch_one("select title, version, status, status_note from items where id=%s", (item_id,))
    assert it is not None
    return {"item": f"item:{item_id}", "title": it["title"], "version": it["version"], "outcome": outcome,
            "ready": it["status"] == "active", **({"note": it["status_note"]} if it["status_note"] else {})}


# Actions behind the sources tool ---------------------------------------------------------------


async def _root_by_ref(ref: str) -> dict | None:
    """A root by its Nebula id, or by its Drive link or id."""
    from nebula.connectors import gdrive

    ref = ref.strip()
    try:
        uuid.UUID(ref)
        row = await db.fetch_one("select * from source_roots where id=%s", (ref,))
        if row:
            return row
    except ValueError:
        pass
    try:
        external = gdrive.drive_id_from(ref)
    except ValueError:
        return None
    space = await drive_space()
    return await db.fetch_one("select * from source_roots where collection_id=%s and external_id=%s",
                              (space["id"], external))


def _can_manage_root(person: Person, root: dict) -> bool:
    return person.is_admin or (root["managed_by"] is not None and str(root["managed_by"]) == person.id)


async def connect(person: Person, link: str, name: str | None = None) -> dict:
    """Start (or restart) importing a Drive folder, file or Shared Drive that's shared with Nebula."""
    from nebula.connectors import gdrive

    bot = _bot()
    if not bot:
        raise ValueError("Drive isn't set up on this Nebula yet. Ask an administrator.")
    fid = gdrive.drive_id_from(link)
    space = await drive_space()
    root = await db.fetch_one("select * from source_roots where collection_id=%s and external_id=%s",
                              (space["id"], fid))
    if root and root["status"] == "active":
        job = await (_queue_file(space, fid, person) if root["kind"] == "file" else _queue_root(space, root, person))
        return await _with_eta({"source": str(root["id"]), "name": root["name"], "kind": root["kind"],
                                "already_connected": True}, job)
    roots = await sync.active_roots(space)
    connector = sync.connector_for(space, roots)
    try:
        meta = await connector.describe(fid)  # type: ignore[attr-defined]
        if meta is None:
            raise ValueError(f"Nebula can't open that in Drive yet. Share it with {bot} {SHARE_AS}. Then try "
                             "again. A link on its own isn't enough: Drive has to share it with that address.")
        kind = "drive" if meta.get("driveId") == fid else "folder" if meta.get("mimeType") == gdrive.FOLDER else "file"
        if kind == "file" and gdrive.classify(meta) is None:
            raise ValueError(f"Nebula can't read '{meta.get('name')}' ({meta.get('mimeType')}). It reads Google Docs, "
                             "Sheets and Slides, PDF, Word, PowerPoint, Excel, CSV and text files up to 50 MB.")
        covering = None
        if kind != "drive" and not (root and root["status"] == "removed"):
            covering = await connector.covering_root(meta, space["sync_cursor"])  # type: ignore[attr-defined]
    finally:
        await connector.close()
    if covering and covering != fid:
        cover = await db.fetch_one("select * from source_roots where collection_id=%s and external_id=%s",
                                   (space["id"], covering))
        assert cover is not None
        job = await (_queue_file(space, fid, person) if kind == "file" else _queue_root(space, cover, person))
        return await _with_eta({"name": meta.get("name", ""), "kind": kind, "already_shared_through": cover["name"],
                                "note": "It's inside something Nebula already reads, so it's being refreshed "
                                        "rather than added separately."}, job)
    from nebula.connectors.gdrive import can_edit

    viewer = can_edit(meta) is False
    async with db.tx() as conn:
        row = await db.fetch_one(
            """insert into source_roots (collection_id, external_id, kind, name, managed_by, added_via, status_note)
               values (%s, %s, %s, %s, %s, 'request', %s)
               on conflict (collection_id, external_id) do update set status='active', status_note=excluded.status_note,
                   kind=excluded.kind, name=excluded.name,
                   managed_by=coalesce(source_roots.managed_by, excluded.managed_by), updated_at=now()
               returning *""",
            (space["id"], fid, kind, name or meta.get("name", ""), person.id, VIEWER_NOTE if viewer else ""), conn)
        assert row is not None
        await org.audit(conn, person, "connect_drive", "source_root", str(row["id"]), {"external_id": fid, "kind": kind})
    job = await (_queue_file(space, fid, person) if kind == "file" else _queue_root(space, row, person))
    out = {"source": str(row["id"]), "name": row["name"], "kind": kind, "how_access_works": ACCESS_NOTE}
    if viewer:
        out["warning"] = VIEWER_NOTE
    return await _with_eta(out, job)


async def _item_by_ref(person: Person, ref: str) -> dict:
    kind, rid = links.parse_ref(ref)
    if kind != "item":
        raise ValueError("Give a file's item id (item:…) or its Drive link.")
    await access.require(person, "item", rid, "view")
    it = await db.fetch_one(
        """select i.*, c.kind as collection_kind, c.slug as collection_slug, c.source_config, c.last_synced_at
           from items i join collections c on c.id = i.collection_id where i.id=%s""", (rid,))
    assert it is not None
    return it


async def _item_by_drive_id(person: Person, external_id: str) -> dict | None:
    rows = await db.fetch_all(
        """select i.*, c.kind as collection_kind, c.slug as collection_slug, c.source_config, c.last_synced_at
           from items i join collections c on c.id = i.collection_id
           where c.kind='gdrive' and c.status='active' and i.external_id=%s
           order by (i.status = 'active') desc, (c.slug = %s) desc""", (external_id, DRIVE_SLUG))
    for it in rows:
        if access.at_least(await access.level_for(person, "item", str(it["id"])), "view"):
            return it
    if rows and rows[0]["status"] == "active":
        raise AccessDenied("Nebula has that file, but Drive doesn't share it with you, so you can't see or refresh it.")
    return None


async def request_sync(person: Person, *, link: str | None = None, item: str | None = None,
                       source: str | None = None) -> dict:
    """Refresh something now: one file (by item id or Drive link), or a whole source."""
    from nebula.connectors import gdrive

    if item or link:
        it = await _item_by_ref(person, item) if item else await _item_by_drive_id(person, gdrive.drive_id_from(link or ""))
        if it is None:
            return await connect(person, link or "")
        if it["collection_kind"] == "notebook":
            raise ValueError("That file was uploaded to Nebula, so it doesn't sync from anywhere. Upload the new "
                             f"version with the file tool (replaces='item:{it['id']}').")
        col = await db.fetch_one("select * from collections where id=%s", (it["collection_id"],))
        assert col is not None
        if col["kind"] == "gdrive":
            job = await _queue_file(col, it["external_id"], person)
        else:  # Notion and Granola are read by time, a whole collection at once
            job = await jobs.enqueue("sync", {"collection_id": str(col["id"])}, dedupe_key=f"sync:{col['id']}",
                                     person_id=person.id, priority=20)
        return await _with_eta({"id": f"item:{it['id']}", "title": it["title"]}, job)
    if source:
        root = await _root_by_ref(source)
        if root:
            if not _can_manage_root(person, root):
                raise AccessDenied("Only the person who shared it with Nebula (or an admin) can re-sync the whole source. "
                                   "To refresh one file, give its link or item id instead.")
            space = await drive_space()
            job = await (_queue_file(space, root["external_id"], person) if root["kind"] == "file"
                         else _queue_root(space, root, person, rewalk=True))
            return await _with_eta({"source": str(root["id"]), "name": root["name"]}, job)
        res = await org.sync_now(person, source)
        return await _with_eta({"collection": source}, res["job"])
    raise ValueError("Give a Drive link, an item id, or a source from action 'list'.")


async def _file_status(person: Person, it: dict) -> dict:
    from nebula.ingest.types import Document

    counts = await db.fetch_one(
        """select count(*) as pieces, count(*) filter (where embedding is null) as waiting
           from chunks where source_type='item' and source_id=%s""", (it["id"],))
    assert counts is not None
    pending = await db.fetch_one(
        """select id from jobs where status in ('queued','running') and kind='sync_item'
           and payload->>'external_id'=%s and payload->>'collection_id'=%s""", (it["external_id"], str(it["collection_id"])))
    audience = it["source_audience"] or {}
    out: dict = {
        "id": f"item:{it['id']}", "title": it["title"], "status": it["status"], "version": it["version"],
        "modified_in_source": it["source_modified_at"], "indexed_at": it["indexed_at"],
        "source_last_checked": it["last_synced_at"],
        "search_ready": it["status"] == "active" and counts["waiting"] == 0,
        "pieces_in_search": counts["pieces"],
    }
    if counts["waiting"]:
        out["pieces_waiting_for_index"] = counts["waiting"]
    if pending:
        out["refresh"] = {"job": str(pending["id"]), **await jobs.eta(str(pending["id"]))}
    if it["status_note"]:
        out["note"] = it["status_note"]
    if audience.get("unknown"):
        out["sharing"] = ("Hidden from everyone but admins: Nebula couldn't read this file's sharing in Drive. "
                          "Drive only shows sharing to editors, so make Nebula's service account an Editor of the "
                          "file or its folder (Nebula still only reads).")
    elif audience:
        bits = [f"{len(audience.get('emails', []))} people"]
        if audience.get("groups"):
            bits.append(f"{len(audience['groups'])} Google Groups")
        if audience.get("domains"):
            bits.append("domain: " + ", ".join(audience["domains"]))
        out["sharing"] = "Follows Drive: " + ", ".join(bits)
        linked = {r["e"] for r in await db.fetch_all(
            "select lower(google_group_email) as e from groups where lower(google_group_email) = any(%s)",
            ([g.lower() for g in audience.get("groups", [])],))}
        unlinked = [g for g in audience.get("groups", []) if g.lower() not in linked]
        if unlinked:
            out["sharing_note"] = ("Drive shares this with Google Groups that aren't linked to a Nebula team yet ("
                                   + ", ".join(unlinked[:5]) + "), so their members can't see it in Nebula. An admin "
                                   "can link them with admin action set_group_email.")
    if it.get("document"):
        summary = items.extraction_summary(Document.from_dict(it["document"]), await items.transcriptions(None, str(it["id"])))
        if summary:
            out["extraction"] = {"status": summary["status"], "parts_not_read": len(summary["parts_not_read"]),
                                 "hint": "open the item to see which parts weren't read"}
    return out


async def _root_status(person: Person, root: dict) -> dict:
    if not _can_manage_root(person, root):
        raise AccessDenied("Only the person who shared it with Nebula (or an admin) can see a whole source's status.")
    space = await drive_space()
    counts = await db.fetch_one(
        """select count(*) filter (where status='active') as active, count(*) filter (where status='deleted') as removed,
                  count(*) filter (where status='active' and source_audience->>'unknown'='true') as hidden
           from items where collection_id=%s and source_root=%s""", (space["id"], root["external_id"]))
    assert counts is not None
    waiting = await db.fetch_one(
        """select count(*) as n from chunks c join items i on c.source_type='item' and c.source_id=i.id
           where i.collection_id=%s and i.source_root=%s and c.embedding is null""", (space["id"], root["external_id"]))
    pending = await db.fetch_one(
        """select id from jobs where status in ('queued','running') and kind in ('sync','sync_item')
           and payload->>'collection_id'=%s order by priority limit 1""", (str(space["id"]),))
    manager = await db.fetch_one("select email from people where id=%s", (root["managed_by"],)) if root["managed_by"] else None
    recent = await db.fetch_all(
        """select id, title, kind, updated_at from items where collection_id=%s and source_root=%s and status='active'
           order by updated_at desc limit 10""", (space["id"], root["external_id"]))
    visible = [r for r in recent if access.at_least(await access.level_for(person, "item", str(r["id"])), "view")]
    out = {
        "source": str(root["id"]), "name": root["name"], "kind": root["kind"], "status": root["status"],
        "managed_by": manager["email"] if manager else None, "shared_by": root["shared_by"] or None,
        "added": root["created_at"], "added_via": root["added_via"],
        "files": {"in_nebula": counts["active"], "removed": counts["removed"]},
        "pieces_waiting_for_index": waiting["n"] if waiting else 0,
        "last_checked": space["last_synced_at"], "sync_error": space["sync_error"],
        "recent": [{"id": f"item:{r['id']}", "title": r["title"], "kind": r["kind"], "updated": r["updated_at"]}
                   for r in visible],
    }
    if root["status_note"]:
        out["note"] = root["status_note"]
    if counts["hidden"]:
        out["hidden_unreadable_sharing"] = counts["hidden"]
        out["hidden_note"] = ("These files are hidden from everyone but admins because Nebula couldn't read their "
                              "sharing in Drive. Drive only shows sharing to editors, so make Nebula's service "
                              "account an Editor (Nebula still only reads).")
    if pending:
        out["sync"] = {"job": str(pending["id"]), **await jobs.eta(str(pending["id"]))}
    return out


async def status(person: Person, *, link: str | None = None, item: str | None = None,
                 source: str | None = None) -> dict:
    from nebula.connectors import gdrive

    if item:
        return await _file_status(person, await _item_by_ref(person, item))
    if link:
        fid = gdrive.drive_id_from(link)
        root = await _root_by_ref(fid)
        if root and root["kind"] != "file":
            return await _root_status(person, root)
        it = await _item_by_drive_id(person, fid)
        if it:
            return await _file_status(person, it)
        pending = await db.fetch_one(
            "select id from jobs where status in ('queued','running') and kind='sync_item' and payload->>'external_id'=%s",
            (fid,))
        out: dict = {"in_nebula": False,
                     "note": f"Nebula doesn't have this yet. If it's shared with {_bot() or 'Nebula'} in Drive it will be "
                             "picked up within a few minutes; use action 'sync' with the link to start now."}
        if pending:
            out["refresh"] = {"job": str(pending["id"]), **await jobs.eta(str(pending["id"]))}
        return out
    if source:
        root = await _root_by_ref(source)
        if root:
            return await _root_status(person, root)
        res = await org.source_status(person, source)
        return res
    return await list_sources(person)


async def list_sources(person: Person) -> dict:
    rows = await db.fetch_all(
        """select r.*, p.email as manager_email,
                  (select count(*) from items i where i.collection_id = r.collection_id
                   and i.source_root = r.external_id and i.status = 'active') as files
           from source_roots r left join people p on p.id = r.managed_by
           where r.status <> 'removed' and (r.managed_by = %s or %s) order by r.created_at desc limit 200""",
        (person.id, person.is_admin))
    legacy = [r for r in await org.list_collections(person)
              if r["kind"] != "notebook" and r["your_access"] == "manage" and r["slug"] != DRIVE_SLUG]
    out = {
        "sources": [{"source": str(r["id"]), "name": r["name"], "kind": r["kind"], "status": r["status"],
                     "files": r["files"], "added": r["created_at"],
                     **({"managed_by": r["manager_email"]} if person.is_admin else {}),
                     **({"note": r["status_note"]} if r["status_note"] else {})} for r in rows],
        "how_to_add": f"Share a Drive folder, file or Shared Drive with {_bot() or 'Nebula'} {SHARE_AS}. "
                      "Nebula picks it up within a few minutes.",
    }
    if legacy:
        out["collections"] = [{k: r[k] for k in ("slug", "name", "kind", "items", "last_synced_at", "sync_error")}
                              for r in legacy]
    return out


async def disconnect(person: Person, source: str) -> dict:
    root = await _root_by_ref(source)
    if root is None:
        return await org.disconnect(person, source)
    if not _can_manage_root(person, root):
        raise AccessDenied("Only the person who shared it with Nebula (or an admin) can disconnect it.")
    space = await drive_space()
    async with db.tx() as conn:
        await conn.execute("update source_roots set status='removed', status_note='', updated_at=now() where id=%s",
                           (root["id"],))
        rows = await db.fetch_all(
            "select external_id from items where collection_id=%s and source_root=%s and status='active'",
            (space["id"], root["external_id"]), conn)
        for r in rows:
            await items.mark_deleted(conn, str(space["id"]), r["external_id"])
        # Anything also inside another shared folder comes back on the next full listing.
        await jobs.enqueue("sync", {"collection_id": str(space["id"]), "full": True}, conn=conn,
                           dedupe_key=f"sync-full:{space['id']}", priority=40)
        await org.audit(conn, person, "disconnect", "source_root", str(root["id"]), {"external_id": root["external_id"]})
    return {"source": str(root["id"]), "name": root["name"], "status": "disconnected", "files_removed": len(rows),
            "note": "Its files are no longer in Nebula, and Nebula won't pick it up again unless someone asks to "
                    "connect it. Nothing in Drive changed; to cut Nebula off completely, unshare it there too."}
