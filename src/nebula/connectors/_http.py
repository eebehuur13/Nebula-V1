"""HTTP plumbing shared by the connectors: retries with backoff and a rate limiter.

Never log request headers here: they carry bearer tokens.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Statuses worth retrying for read requests. 529 is Notion's "service overloaded".
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
MAX_RETRIES = 5
MAX_RETRY_AFTER = 120.0

# Module-level so tests can replace it with an instant fake.
sleep = asyncio.sleep


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff with jitter: half fixed, half random."""
    delay = min(cap, base * (2**attempt))
    return delay / 2 + random.uniform(0, delay / 2)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Seconds from a Retry-After header (integer form), capped."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(value)))
    except ValueError:
        return None


class RateLimiter:
    """Spaces requests out to at most `per_second` on average (no bursts)."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if not self.interval:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            self._next_at = max(now, self._next_at) + self.interval
        if delay > 0:
            await sleep(delay)


async def send_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retry_if: Callable[[httpx.Response], bool] | None = None,
    limiter: RateLimiter | None = None,
    max_retries: int = MAX_RETRIES,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying rate limits, server errors and network errors.

    Returns the last response (even an error one); callers decide what to raise.
    """
    should_retry = retry_if or (lambda r: r.status_code in RETRY_STATUSES)
    for attempt in range(max_retries + 1):
        if limiter:
            await limiter.wait()
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                raise
            delay = backoff_delay(attempt)
            log.warning("%s %s failed (%s); retrying in %.1fs", method, _safe(url), type(exc).__name__, delay)
            await sleep(delay)
            continue
        if attempt >= max_retries or not should_retry(response):
            return response
        delay = retry_after_seconds(response)
        if delay is None:
            delay = backoff_delay(attempt)
        log.warning("%s %s -> %s; retrying in %.1fs", method, _safe(url), response.status_code, delay)
        await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _safe(url: str) -> str:
    """URL without its query string (queries can be long; never contain secrets here, but keep logs short)."""
    return url.split("?", 1)[0]


def parse_time(value: str | None) -> datetime | None:
    """ISO 8601 timestamp (with Z or offset) to an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class SourceError(RuntimeError):
    """A source API answered with an error we can't recover from by retrying."""

    def __init__(self, source: str, response: httpx.Response):
        self.status_code = response.status_code
        body = response.text[:300] if response.content else ""
        request = response.request
        super().__init__(f"{source} API {request.method} {_safe(str(request.url))} -> {response.status_code}: {body}")
