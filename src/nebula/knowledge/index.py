"""Writing to the search index: chunks in, embeddings filled in by the worker."""

from __future__ import annotations

import logging
from typing import Iterable

from psycopg import AsyncConnection

from nebula import db, jobs, models
from nebula.ingest.types import Chunk

log = logging.getLogger(__name__)


async def replace_chunks(
    conn: AsyncConnection,
    source_type: str,
    source_id: str,
    collection_id: str | None,
    chunks: Iterable[Chunk],
) -> int:
    """Swap a source's chunks for new ones (embeddings are added later by the worker).

    A new chunk with the same heading and text as an old one keeps the old embedding, so a small
    edit to a long document only re-embeds the pieces that actually changed.
    """
    old = await db.fetch_all("select id from chunks where source_type = %s and source_id = %s",
                             (source_type, source_id), conn)
    old_ids = [r["id"] for r in old]
    rows = [
        (source_type, source_id, collection_id, i, c.kind, " › ".join(c.heading_path)[:500], c.text, db.jsonb(c.locator))
        for i, c in enumerate(chunks)
        if c.text.strip()
    ]
    if rows:
        async with conn.cursor() as cur:
            await cur.executemany(
                """insert into chunks (source_type, source_id, collection_id, seq, kind, heading, text, locator)
                   values (%s, %s, %s, %s, %s, %s, %s, %s)""",
                rows,
            )
    if old_ids:
        if rows:
            await conn.execute(
                """update chunks n set embedding = o.embedding, embed_model = o.embed_model
                   from chunks o
                   where n.source_type = %(t)s and n.source_id = %(s)s and n.embedding is null
                     and n.id <> all(%(old)s) and o.id = any(%(old)s) and o.embedding is not null
                     and o.embed_model = %(model)s and o.heading = n.heading and o.text = n.text""",
                {"t": source_type, "s": source_id, "old": old_ids, "model": models.embed_model_name()},
            )
        await conn.execute("delete from chunks where id = any(%s)", (old_ids,))
    pending = await db.fetch_one(
        "select exists (select 1 from chunks where source_type = %s and source_id = %s and embedding is null) as p",
        (source_type, source_id), conn)
    if pending and pending["p"]:
        await jobs.enqueue("embed", {}, conn=conn, dedupe_key="embed", priority=50)
    return len(rows)


async def drop_chunks(conn: AsyncConnection, source_type: str, source_id: str) -> None:
    await conn.execute("delete from chunks where source_type = %s and source_id = %s", (source_type, source_id))


async def embed_pending(batch: int = 64, max_batches: int = 50, *, source: tuple[str, str] | None = None) -> int:
    """Fill in missing embeddings (all of them, or one source's). Returns how many chunks were embedded."""
    done = 0
    model = models.embed_model_name()
    only = "and source_type = %s and source_id = %s" if source else ""
    for _ in range(max_batches):
        async with db.tx() as conn:
            rows = await db.fetch_all(
                f"""select id, heading, text from chunks where embedding is null {only}
                   order by id limit %s for update skip locked""",
                (*(source or ()), batch),
                conn,
            )
            if not rows:
                break
            texts = [(r["heading"] + "\n" + r["text"]).strip()[:8000] for r in rows]
            vectors = await models.embed(texts, "document")
            async with conn.cursor() as cur:
                await cur.executemany(
                    "update chunks set embedding = %s::halfvec, embed_model = %s where id = %s",
                    [(v, model, r["id"]) for v, r in zip(vectors, rows)],
                )
            done += len(rows)
    return done


@jobs.handler("embed")
async def _embed_job(job: dict, payload: dict) -> dict:
    n = await embed_pending()
    remaining = await db.fetch_one("select count(*) as n from chunks where embedding is null")
    if remaining and remaining["n"]:
        await jobs.enqueue("embed", {}, dedupe_key="embed-next", priority=60)
    return {"embedded": n}
