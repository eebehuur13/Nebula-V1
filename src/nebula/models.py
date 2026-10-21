"""Embedding and reranking models, behind one small interface each.

Providers:
- bedrock-cohere: Cohere Embed v4 through the US cross-region profile; Cohere Rerank 3.5 in-region.
- bedrock-titan: Titan Text Embeddings V2 (in-region, English-optimised).
- local: multilingual-e5-large and bge-reranker-v2-m3 via fastembed (ONNX, CPU). Used while
  Bedrock is unavailable on the account, and in development.

Every stored vector records its model name, so switching providers means re-embedding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from functools import lru_cache
from typing import Literal

from nebula.config import settings

log = logging.getLogger(__name__)

InputType = Literal["document", "query"]

# The local models are large (XLM-R size), and a model call's working memory grows with its batch.
# Measured on 21 Oct: reranking 40 candidates in one batch of 64 took +2.5 GB; in batches of 8,
# +0.14 GB at the same speed. Two overlapping searches at batch 64 ran the 6 GB API task out of
# memory on 20 Oct. So batches stay small, and one local model call runs at a time per process
# (on one vCPU, running them side by side gains nothing).
EMBED_BATCH = 4
RERANK_BATCH = 8
_local_model_lock = threading.Lock()


def embed_model_name() -> str:
    s = settings()
    return {
        "bedrock-cohere": "bedrock:cohere.embed-v4:1024",
        "bedrock-titan": "bedrock:amazon.titan-embed-text-v2:1024",
        "local": f"local:{s.local_embed_model}",
        "fake": "fake:hash-1024",
    }[s.embed_provider]


@lru_cache
def _bedrock():
    import boto3

    return boto3.client("bedrock-runtime", region_name=settings().aws_region)


@lru_cache
def _bedrock_agent():
    import boto3

    return boto3.client("bedrock-agent-runtime", region_name=settings().aws_region)


@lru_cache
def _local_embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(settings().local_embed_model)


@lru_cache
def _local_reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(settings().local_rerank_model)


def _fake_vector(text: str, dim: int) -> list[float]:
    """Deterministic bag-of-words hashing vector, for tests only (no semantics beyond shared words)."""
    import hashlib
    import math
    import re

    v = [0.0] * dim
    for w in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % dim] += 1.0 if (h >> 20) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _embed_sync(texts: list[str], input_type: InputType) -> list[list[float]]:
    s = settings()
    if not texts:
        return []
    if s.embed_provider == "fake":
        return [_fake_vector(t, s.embed_dim) for t in texts]
    if s.embed_provider == "local":
        prefix = "query: " if input_type == "query" else "passage: "
        with _local_model_lock:
            return [v.tolist() for v in _local_embedder().embed([prefix + t for t in texts], batch_size=EMBED_BATCH)]
    if s.embed_provider == "bedrock-cohere":
        out: list[list[float]] = []
        for i in range(0, len(texts), 96):
            body = {
                "texts": [t[:20000] for t in texts[i : i + 96]],
                "input_type": "search_query" if input_type == "query" else "search_document",
                "embedding_types": ["float"],
                "output_dimension": s.embed_dim,
                "truncate": "END",
            }
            resp = _bedrock().invoke_model(
                modelId="us.cohere.embed-v4:0", body=json.dumps(body), contentType="application/json"
            )
            out += json.loads(resp["body"].read())["embeddings"]["float"]
        return out
    # bedrock-titan: one text per call
    vecs = []
    for t in texts:
        resp = _bedrock().invoke_model(
            modelId="amazon.titan-embed-text-v2:0",
            body=json.dumps({"inputText": t[:40000], "dimensions": s.embed_dim, "normalize": True}),
            contentType="application/json",
        )
        vecs.append(json.loads(resp["body"].read())["embedding"])
    return vecs


async def embed(texts: list[str], input_type: InputType = "document") -> list[list[float]]:
    return await asyncio.to_thread(_embed_sync, texts, input_type)


def _rerank_sync(query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
    s = settings()
    if not docs:
        return []
    if s.rerank_provider == "none":
        return [(i, 1.0 / (i + 1)) for i in range(min(top_n, len(docs)))]
    if s.rerank_provider == "local":
        with _local_model_lock:
            scores = list(_local_reranker().rerank(query, [d[:2000] for d in docs], batch_size=RERANK_BATCH))
        order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
        return [(i, float(scores[i])) for i in order[:top_n]]
    resp = _bedrock_agent().rerank(
        queries=[{"type": "TEXT", "textQuery": {"text": query[:2000]}}],
        sources=[
            {"type": "INLINE", "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": d[:4000]}}}
            for d in docs
        ],
        rerankingConfiguration={
            "type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {
                "numberOfResults": min(top_n, len(docs)),
                "modelConfiguration": {
                    "modelArn": f"arn:aws:bedrock:{s.aws_region}::foundation-model/cohere.rerank-v3-5:0"
                },
            },
        },
    )
    return [(r["index"], float(r["relevanceScore"])) for r in resp["results"]]


async def rerank(query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
    """Indices into `docs`, best first, with scores. Falls back to input order on errors."""
    try:
        return await asyncio.to_thread(_rerank_sync, query, docs, top_n)
    except Exception as e:  # a reranker outage must not break search
        log.warning("rerank failed, keeping fused order: %s", e)
        return [(i, 1.0 / (i + 1)) for i in range(min(top_n, len(docs)))]


def warm() -> None:
    """Load local models at startup so the first search isn't slow."""
    s = settings()
    if s.embed_provider == "local":
        _embed_sync(["warm up"], "query")
    if s.rerank_provider == "local":
        _rerank_sync("warm", ["up"], 1)
