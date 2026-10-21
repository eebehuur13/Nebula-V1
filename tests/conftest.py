"""Test settings: a separate local database, fake embeddings, dev sign-in, files on disk."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("NEBULA_DATABASE_URL", "postgresql://nebula:nebula@localhost:5433/nebula_test")
os.environ["NEBULA_EMBED_PROVIDER"] = "fake"
os.environ["NEBULA_RERANK_PROVIDER"] = "none"
os.environ["NEBULA_AUTH_MODE"] = "dev"
os.environ["NEBULA_ADMIN_EMAILS"] = "admin@example.org"
os.environ["NEBULA_LOCAL_STORAGE_DIR"] = tempfile.mkdtemp(prefix="nebula-test-files-")
os.environ["NEBULA_PUBLIC_URL"] = "http://testserver"

sys.path.insert(0, str(Path(__file__).parent))  # so test modules can import helpers


@pytest.fixture(scope="session")
async def fresh_db():
    """One empty database for the whole run; the database-backed modules share it in file order."""
    from psycopg import AsyncConnection

    from nebula import db
    from nebula.config import settings

    async with await AsyncConnection.connect(settings().dsn, autocommit=True) as conn:
        await conn.execute("drop schema if exists public cascade; drop schema if exists data cascade; "
                           "create schema public;")
    await db.close()
    await db.migrate()
    yield
    await db.close()


@pytest.fixture(scope="session")
async def mcp(fresh_db):
    from nebula import server, web, webui  # noqa: F401

    return server.mcp
