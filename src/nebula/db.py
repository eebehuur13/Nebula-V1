"""Database access: one async connection pool, migrations, and small helpers."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any, AsyncIterator

from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from nebula.config import settings

log = logging.getLogger(__name__)

_pool: AsyncConnectionPool | None = None


async def _configure(conn: AsyncConnection) -> None:
    try:
        await register_vector_async(conn)
    except Exception:  # extension not created yet (first boot, before migrations)
        pass
    await conn.set_autocommit(False)


async def pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            settings().dsn,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            configure=_configure,
            open=False,
        )
        await _pool.open(wait=True, timeout=30)
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def tx() -> AsyncIterator[AsyncConnection]:
    """A connection inside a transaction; commits on success, rolls back on error."""
    p = await pool()
    async with p.connection() as conn:
        async with conn.transaction():
            yield conn


async def fetch_all(sql: str, params: Any = None, conn: AsyncConnection | None = None) -> list[dict]:
    if conn is not None:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()
    async with tx() as c:
        cur = await c.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(sql: str, params: Any = None, conn: AsyncConnection | None = None) -> dict | None:
    rows = await fetch_all(sql, params, conn)
    return rows[0] if rows else None


async def execute(sql: str, params: Any = None, conn: AsyncConnection | None = None) -> None:
    if conn is not None:
        await conn.execute(sql, params)
        return
    async with tx() as c:
        await c.execute(sql, params)


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


async def migrate() -> list[str]:
    """Apply pending SQL migrations in order. Safe to call from several processes."""
    applied: list[str] = []
    async with await AsyncConnection.connect(settings().dsn, autocommit=True) as conn:
        await conn.execute("select pg_advisory_lock(727274)")
        try:
            await conn.execute(
                "create table if not exists schema_migrations ("
                " version text primary key, applied_at timestamptz not null default now())"
            )
            cur = await conn.execute("select version from schema_migrations")
            done = {r[0] for r in await cur.fetchall()}
            files = sorted(
                f for f in resources.files("nebula.migrations").iterdir() if f.name.endswith(".sql")
            )
            for f in files:
                version = f.name.removesuffix(".sql")
                if version in done:
                    continue
                log.info("applying migration %s", version)
                async with conn.transaction():
                    await conn.execute(f.read_text())
                    await conn.execute("insert into schema_migrations (version) values (%s)", (version,))
                applied.append(version)
        finally:
            await conn.execute("select pg_advisory_unlock(727274)")
    return applied
