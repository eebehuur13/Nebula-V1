"""Atomic account limits. No model API or extra cloud service is involved."""
from __future__ import annotations

import datetime as dt

from nebula import db
from nebula.config import settings


class LimitExceeded(ValueError):
    pass


async def consume(person_id: str, metric: str, *, amount: int = 1) -> int:
    now = dt.datetime.now(dt.UTC)
    if metric == "tool_calls":
        start, cap = now.replace(second=0, microsecond=0), settings().tool_calls_per_minute
    elif metric == "writes":
        start, cap = now.replace(hour=0, minute=0, second=0, microsecond=0), settings().writes_per_day
    else:
        raise ValueError("Unknown usage metric")
    if amount < 1:
        raise ValueError("Usage must be positive")
    row = await db.fetch_one(
        """insert into usage_counters(person_id,metric,window_start,used) select %s,%s,%s,%s where %s <= %s
           on conflict(person_id,metric,window_start) do update set used=usage_counters.used+excluded.used
           where usage_counters.used+excluded.used <= %s returning used""",
        (person_id, metric, start, amount, amount, cap, cap),
    )
    if not row:
        period = "this minute" if metric == "tool_calls" else "today"
        raise LimitExceeded(f"Your account's {metric.replace('_', ' ')} limit is reached for {period}. Try later or contact the administrator.")
    return row["used"]


async def reserve_storage(person_id: str, key: str, size: int) -> tuple[str, int]:
    """Charge physical bytes before storing them. A repeated key is not charged twice."""
    if size < 0:
        raise ValueError("Negative storage size")
    async with db.tx() as conn:
        await conn.execute("insert into storage_accounts(person_id) values(%s) on conflict do nothing", (person_id,))
        account = await db.fetch_one("select used_bytes from storage_accounts where person_id=%s for update", (person_id,), conn)
        old = await db.fetch_one("select person_id,size_bytes from stored_objects where file_key=%s for update", (key,), conn)
        if old and str(old["person_id"]) != person_id:
            raise LimitExceeded("This storage key belongs to another account.")
        before = old["size_bytes"] if old else 0
        delta = size - before
        if account["used_bytes"] + delta > settings().storage_bytes_per_person:
            raise LimitExceeded("Your account's stored-file limit is reached. Contact the administrator before uploading more.")
        stored = await db.fetch_one(
            "insert into stored_objects(file_key,person_id,size_bytes) values(%s,%s,%s) "
            "on conflict(file_key) do update set size_bytes=excluded.size_bytes,updated_at=now() "
            "where stored_objects.person_id=excluded.person_id returning person_id",
            (key, person_id, size), conn)
        if not stored:
            raise LimitExceeded("This storage key belongs to another account.")
        await conn.execute("update storage_accounts set used_bytes=used_bytes+%s where person_id=%s", (delta, person_id))
    return person_id, before


async def release_storage(key: str) -> None:
    async with db.tx() as conn:
        # Match reserve's account-before-object lock order.
        entry = await db.fetch_one("select person_id from stored_objects where file_key=%s", (key,), conn)
        if not entry:
            return
        await conn.execute("select person_id from storage_accounts where person_id=%s for update", (entry["person_id"],))
        removed = await db.fetch_one("delete from stored_objects where file_key=%s returning person_id,size_bytes", (key,), conn)
        if removed:
            await conn.execute("update storage_accounts set used_bytes=greatest(0,used_bytes-%s) where person_id=%s",
                               (removed["size_bytes"], removed["person_id"]))


async def usage(person_id: str) -> dict:
    row = await db.fetch_one("select used_bytes from storage_accounts where person_id=%s", (person_id,))
    return {"stored_bytes": row["used_bytes"] if row else 0,
            "storage_limit_bytes": settings().storage_bytes_per_person,
            "max_upload_bytes": settings().max_upload_bytes,
            "tool_calls_per_minute": settings().tool_calls_per_minute,
            "writes_per_day": settings().writes_per_day}
