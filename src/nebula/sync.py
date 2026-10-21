"""Keeping collections in step with their sources.

A sync job reads changes since the collection's cursor and brings each changed item up to date.
For Drive, a change carries the file's revision marker and a fingerprint of its sharing, so a file
whose content didn't change isn't downloaded again, and its sharing is only re-read when the
fingerprint changed or a folder above it was re-shared.

A full listing (the first run, then hourly for Drive and daily elsewhere) also marks items that no
longer exist as deleted. Once a day the full listing re-reads every file's sharing regardless of
fingerprints, which catches sharing inherited from folders above a root.

The nightly audience check compares a collection's Nebula audience with who can open the root in
the source, and records any widening for the owner to fix.
"""

from __future__ import annotations

import datetime as dt
import logging

from nebula import access, db, jobs
from nebula.connectors import get_connector
from nebula.connectors.base import Change, Connector
from nebula.knowledge import items

log = logging.getLogger(__name__)

FULL_SYNC_EVERY = {"gdrive": dt.timedelta(hours=1)}   # listing only: unchanged files aren't fetched
DEFAULT_FULL_SYNC_EVERY = dt.timedelta(hours=24)
ACL_SWEEP_EVERY = dt.timedelta(hours=24)
SYNC_EVERY_MINUTES = {"gdrive": 2}                     # an idle Drive check is one API call
DEFAULT_SYNC_EVERY_MINUTES = 15
MAX_ITEMS_PER_JOB = 200
LOST_NOTE = ("Nebula can no longer open this in Drive: it was unshared from Nebula, moved to the trash or "
             "deleted. Share it with Nebula again to bring it back.")


def is_space(col: dict) -> bool:
    """The one Drive collection that holds everything shared with Nebula, root by root."""
    return col.get("kind") == "gdrive" and (col.get("source_config") or {}).get("space") is True


async def active_roots(col: dict) -> list[dict]:
    return await db.fetch_all(
        "select id, external_id, kind, name, managed_by from source_roots where collection_id=%s and status='active' "
        "order by created_at", (col["id"],))


def connector_for(col: dict, roots: list[dict] | None = None, rewalk: list[str] | None = None) -> Connector:
    config = dict(col.get("source_config") or {})
    if is_space(col):
        config["roots"] = [{"id": r["external_id"], "kind": r["kind"]} for r in roots or []]
        if rewalk:
            config["rewalk"] = rewalk
    return get_connector(col["kind"], config)


def managers_of(roots: list[dict]) -> dict[str, str]:
    """Root external id -> the person whose storage its files count against."""
    return {r["external_id"]: str(r["managed_by"]) for r in roots if r["managed_by"]}


async def process_change(col: dict, connector: Connector, ch: Change, *, full: bool = False, sweep: bool = False,
                         charge_to: str | None = None, refetch: bool = False) -> tuple[str, str | None]:
    """Bring one item up to date. Returns (changed | unchanged | deleted | skipped, item id)."""
    cid = str(col["id"])
    mirror = access.mirrors_source(col)
    if ch.deleted:
        async with db.tx() as conn:
            gone = await items.mark_deleted(conn, cid, ch.external_id)
        return ("deleted" if gone else "unchanged"), gone
    if ch.narrower_than_root and not mirror:
        # Shared more narrowly than the collection's audience, which would widen it here.
        async with db.tx() as conn:
            await items.mark_deleted(conn, cid, ch.external_id)
        return "skipped", None
    existing = await db.fetch_one(
        "select id, status, source_rev, acl_fingerprint from items where collection_id=%s and external_id=%s",
        (cid, ch.external_id))
    if existing and existing["status"] == "active" and ch.rev and existing["source_rev"] == ch.rev and not refetch:
        # Same content as stored: keep names and paths current, and re-read sharing only if it may have changed.
        item_id = str(existing["id"])
        recheck = mirror and (sweep or ch.recheck_acl or (ch.acl is None and full)
                              or (ch.acl is not None and ch.acl != existing["acl_fingerprint"]))
        audience = await connector.file_audience(ch.external_id) if recheck else None  # type: ignore[attr-defined]
        async with db.tx() as conn:
            await conn.execute(
                """update items set title=coalesce(nullif(%s, ''), title), url=coalesce(nullif(%s, ''), url), path=%s,
                          source_root=coalesce(%s, source_root), source_modified_at=coalesce(%s, source_modified_at)
                   where id=%s""",
                (ch.title, ch.url, ch.path, ch.root, ch.modified_at, item_id))
            if audience is not None:
                await items.apply_source_audience(conn, col, item_id, audience)
                await conn.execute("update items set acl_fingerprint=%s where id=%s", (ch.acl, item_id))
        return "unchanged", item_id
    fetched = await connector.fetch(ch)
    if fetched is None:
        return "skipped", str(existing["id"]) if existing else None
    async with db.tx() as conn:
        res = await items.upsert(conn, cid, fetched, charge_to=charge_to)
        await conn.execute(
            "update items set source_rev=%s, acl_fingerprint=%s, source_root=coalesce(%s, source_root) where id=%s",
            (fetched.extra.get("rev", ch.rev) or None, fetched.extra.get("acl", ch.acl), ch.root, res["id"]))
        if "audience" in fetched.extra:
            await items.apply_source_audience(conn, col, res["id"], fetched.extra["audience"])
    return ("changed" if res["changed"] else "unchanged"), res["id"]


async def roots_lost(col: dict, root_ids: list[str]) -> None:
    """Roots the source no longer lets us read: mark them and take their files out of Nebula."""
    if not is_space(col):
        raise ValueError("Nebula can no longer read this Drive folder. Check its sharing with the service account.")
    cid = str(col["id"])
    async with db.tx() as conn:
        await conn.execute(
            """update source_roots set status='lost', status_note=%s, updated_at=now()
               where collection_id=%s and external_id = any(%s) and status='active'""",
            (LOST_NOTE, cid, root_ids))
        rows = await db.fetch_all(
            "select external_id from items where collection_id=%s and source_root = any(%s) and status='active'",
            (cid, root_ids), conn)
        for r in rows:
            await items.mark_deleted(conn, cid, r["external_id"])


@jobs.handler("sync")
async def sync_collection(job: dict, payload: dict) -> dict:
    col = await db.fetch_one("select * from collections where id = %s", (payload["collection_id"],))
    if not col or col["status"] != "active" or col["kind"] == "notebook":
        return {"skipped": True}
    now = dt.datetime.now(dt.UTC)
    every = FULL_SYNC_EVERY.get(col["kind"], DEFAULT_FULL_SYNC_EVERY)
    full = bool(payload.get("full")) or col["sync_cursor"] is None or (
        col["last_full_sync_at"] is None or now - col["last_full_sync_at"] > every
    )
    mirror = access.mirrors_source(col)
    sweep = bool(payload["sweep"]) if "sweep" in payload else (
        full and mirror and (col["last_acl_sweep_at"] is None or now - col["last_acl_sweep_at"] > ACL_SWEEP_EVERY))
    cursor = None if full else col["sync_cursor"]
    if payload.get("resume_cursor"):
        cursor = payload["resume_cursor"]
    seen: set[str] = set(payload.get("seen") or [])
    roots = await active_roots(col) if is_space(col) else []
    managers = managers_of(roots)
    connector = connector_for(col, roots, rewalk=payload.get("rewalk"))
    stats = {"changed": 0, "unchanged": 0, "deleted": 0, "skipped": 0, "errors": 0}
    processed = 0
    try:
        while True:
            batch = await connector.list_changes(cursor)
            if batch.lost_roots:
                await roots_lost(col, batch.lost_roots)
                stats["lost_roots"] = stats.get("lost_roots", 0) + len(batch.lost_roots)
            for ch in batch.changes:
                seen.add(ch.external_id)
                try:
                    outcome, _ = await process_change(col, connector, ch, full=full, sweep=sweep,
                                                      charge_to=managers.get(ch.root or ""))
                    stats[outcome] += 1
                except Exception as e:  # one bad item must not stop the collection
                    stats["errors"] += 1
                    log.warning("sync %s: item %s failed: %s", col["slug"], ch.external_id, e)
                processed += 1
            cursor = batch.next_cursor
            if not batch.has_more:
                break
            if processed >= MAX_ITEMS_PER_JOB:
                # Continue in a fresh job so progress is saved and other work (like a file someone
                # asked for) can run in between.
                await jobs.enqueue("sync", {"collection_id": str(col["id"]), "full": full, "sweep": sweep,
                                            "resume_cursor": cursor, "seen": sorted(seen) if full else []},
                                   dedupe_key=f"sync-cont:{col['id']}", priority=90)
                return {**stats, "continued": True}
        if full:
            gone = await db.fetch_all(
                "select external_id from items where collection_id=%s and status='active'", (col["id"],)
            )
            async with db.tx() as conn:
                for g in gone:
                    if g["external_id"] not in seen:
                        await items.mark_deleted(conn, str(col["id"]), g["external_id"])
                        stats["deleted"] += 1
        await db.execute(
            """update collections set sync_cursor=%s, last_synced_at=now(), sync_error=null,
                      last_full_sync_at = case when %s then now() else last_full_sync_at end,
                      last_acl_sweep_at = case when %s then now() else last_acl_sweep_at end
               where id=%s""",
            (cursor, full, sweep, col["id"]),
        )
        return stats
    except Exception as e:
        await db.execute("update collections set sync_error=%s where id=%s", (str(e)[:1000], col["id"]))
        raise
    finally:
        await connector.close()


@jobs.handler("audience_check")
async def audience_check(job: dict, payload: dict) -> dict:
    """Flag collections whose Nebula audience is wider than who can open the source root."""
    results = {}
    for col in await db.fetch_all("select * from collections where status='active' and kind <> 'notebook'"):
        if is_space(col):
            continue  # every file mirrors its own Drive sharing; there's no collection audience to widen
        connector = get_connector(col["kind"], col["source_config"] or {})
        try:
            src = await connector.source_audience()
        except Exception as e:
            results[col["slug"]] = f"error: {e}"
            continue
        finally:
            await connector.close()
        groups = await db.fetch_all(
            """select g.id, g.google_group_email from grants gr join groups g on g.id = gr.principal_id
               where gr.resource_type='collection' and gr.resource_id=%s and gr.principal_type='group'""",
            (col["id"],),
        )
        wider = []
        if not src.unknown and not src.domain_wide and not src.public:
            for g in groups:
                if g["id"] == "all_rl":
                    wider.append("All RL can see it in Nebula, but the source isn't shared domain-wide")
                elif g["google_group_email"] and g["google_group_email"].lower() not in {x.lower() for x in src.groups}:
                    wider.append(f"group {g['id']} isn't on the source's sharing list")
        check = {"checked_at": dt.datetime.now(dt.UTC).isoformat(), "unknown": src.unknown, "note": src.note,
                 "wider_than_source": wider}
        await db.execute("update collections set audience_check=%s where id=%s", (db.jsonb(check), col["id"]))
        results[col["slug"]] = check
    return results


async def schedule_due_syncs() -> int:
    """Queue a sync for every synced collection that's due (Drive every couple of minutes, others every
    15), unless one is already queued or running."""
    rows = await db.fetch_all(
        """select c.id from collections c
           where c.status='active' and c.kind <> 'notebook'
             and (c.last_synced_at is null or c.last_synced_at < now() - make_interval(
                    mins => case c.kind when 'gdrive' then %s else %s end))
             and (coalesce((c.source_config->>'space')::boolean, false) = false
                  or exists (select 1 from source_roots r where r.collection_id = c.id and r.status = 'active'))
             and not exists (select 1 from jobs j where j.kind = 'sync' and j.status in ('queued', 'running')
                             and j.payload->>'collection_id' = c.id::text)""",
        (SYNC_EVERY_MINUTES["gdrive"], DEFAULT_SYNC_EVERY_MINUTES),
    )
    for r in rows:
        await jobs.enqueue("sync", {"collection_id": str(r["id"])}, dedupe_key=f"sync:{r['id']}", priority=80)
    return len(rows)
