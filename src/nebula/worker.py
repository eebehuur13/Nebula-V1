"""The background worker: runs queued jobs and the periodic housekeeping."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import signal

from nebula import db, jobs, models
from nebula.config import settings

# Importing these registers their job handlers.
from nebula import files, server, sources, sync  # noqa: F401
from nebula.knowledge import index  # noqa: F401

log = logging.getLogger(__name__)


async def _housekeeping(last: dict[str, dt.datetime]) -> None:
    now = dt.datetime.now(dt.UTC)

    def due(name: str, every: dt.timedelta) -> bool:
        if now - last.get(name, dt.datetime.min.replace(tzinfo=dt.UTC)) >= every:
            last[name] = now
            return True
        return False

    if due("syncs", dt.timedelta(minutes=1)):
        await sync.schedule_due_syncs()
    if due("discover", dt.timedelta(minutes=2)):
        # Anything newly shared with Nebula in Drive becomes a source on its own; one API call when nothing changed.
        from nebula.connectors.gdrive import service_account_email

        if service_account_email():
            await jobs.enqueue("drive_discover", {}, dedupe_key="drive_discover", priority=60)
    if due("embed", dt.timedelta(minutes=2)):
        await jobs.enqueue("embed", {}, dedupe_key="embed", priority=50)
    if due("stale", dt.timedelta(minutes=10)):
        await jobs.requeue_stale()
    if due("uploads", dt.timedelta(minutes=10)):
        await db.execute("update uploads set status='expired' where status='pending' and created_at < now() - interval '1 day'")
    if due("audience", dt.timedelta(hours=24)):
        await jobs.enqueue("audience_check", {}, dedupe_key="audience_check", priority=150)


async def run_forever() -> None:
    await db.migrate()
    await db.pool()
    await asyncio.to_thread(models.warm)
    server.load_gap_tools()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass
    last: dict[str, dt.datetime] = {}
    log.info("worker started")
    while not stop.is_set():
        try:
            await _housekeeping(last)
            worked = await jobs.run_one()
        except Exception:
            log.exception("worker loop error")
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings().worker_poll_seconds)
            except TimeoutError:
                pass
    await db.close()
    log.info("worker stopped")
