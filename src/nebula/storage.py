"""Original files and outputs: S3 on AWS, a local folder in development.

Claude's sandbox never talks to S3 directly. It gets short-lived signed links on
Nebula's own domain (`/files/<token>`), so people only allow one domain in Claude's
code-execution settings. The API streams those requests to and from storage.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from functools import lru_cache
from pathlib import Path

from nebula.config import settings


@lru_cache
def _s3():
    import boto3

    return boto3.client("s3", region_name=settings().aws_region)


def _local_path(key: str) -> Path:
    p = Path(settings().local_storage_dir) / key
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _put_sync(key: str, data: bytes, mime: str) -> None:
    s = settings()
    if s.s3_bucket:
        _s3().put_object(Bucket=s.s3_bucket, Key=key, Body=data, ContentType=mime or "application/octet-stream")
    else:
        _local_path(key).write_bytes(data)


def _get_sync(key: str) -> bytes:
    s = settings()
    if s.s3_bucket:
        return _s3().get_object(Bucket=s.s3_bucket, Key=key)["Body"].read()
    return _local_path(key).read_bytes()


def _exists_sync(key: str) -> int | None:
    """Size in bytes if the object exists."""
    s = settings()
    if s.s3_bucket:
        try:
            return _s3().head_object(Bucket=s.s3_bucket, Key=key)["ContentLength"]
        except Exception:
            return None
    p = Path(s.local_storage_dir) / key
    return p.stat().st_size if p.exists() else None


async def put(key: str, data: bytes, mime: str = "", *, person_id: str | None = None) -> None:
    from nebula import limits

    previous = await limits.reserve_storage(person_id, key, len(data)) if person_id else None
    try:
        await asyncio.to_thread(_put_sync, key, data, mime)
    except Exception:
        if previous:
            if previous[1]:
                await limits.reserve_storage(previous[0], key, previous[1])
            else:
                await limits.release_storage(key)
        raise


async def _delete(key: str) -> None:
    if settings().s3_bucket:
        await asyncio.to_thread(_s3().delete_object, Bucket=settings().s3_bucket, Key=key)
    else:
        await asyncio.to_thread(_local_path(key).unlink, missing_ok=True)
    from nebula import limits
    await limits.release_storage(key)


async def delete_temporary(key: str) -> None:
    """Remove only staged duplicates after their canonical file is safely stored."""
    if not key.startswith(("uploads/", "source-staging/")):
        raise ValueError("Only temporary uploads can be removed here")
    await _delete(key)


async def discard_unreferenced(key: str) -> None:
    """Remove a file no item or version points to, e.g. one stored by a transaction that then failed."""
    from nebula import db

    if await db.fetch_one("select 1 from items where file_key=%s union all "
                          "select 1 from item_versions where file_key=%s limit 1", (key, key)):
        return
    await _delete(key)


async def get(key: str) -> bytes:
    return await asyncio.to_thread(_get_sync, key)


async def exists(key: str) -> int | None:
    return await asyncio.to_thread(_exists_sync, key)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Signed links on Nebula's own domain -------------------------------------------------


def _sign(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    mac = hmac.new(settings().jwt_signing_key.encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(mac).decode().rstrip("=")


def verify_token(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        mac = hmac.new(settings().jwt_signing_key.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(base64.urlsafe_b64encode(mac).decode().rstrip("="), sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def signed_url(key: str, *, method: str, filename: str = "", mime: str = "", ttl: int = 900,
               upload_id: str | None = None, max_bytes: int | None = None, person_id: str | None = None,
               resource_type: str | None = None, resource_id: str | None = None) -> str:
    payload = {"k": key, "m": method, "f": filename, "t": mime, "exp": int(time.time()) + ttl}
    if upload_id:
        payload["u"] = upload_id
    if person_id:
        payload["p"] = person_id
    if resource_type and resource_id:
        payload["r"] = [resource_type, resource_id]
    if method == "PUT":
        payload["max"] = min(max_bytes or settings().max_upload_bytes, settings().max_upload_bytes)
    return f"{settings().public_url.rstrip('/')}/files/{_sign(payload)}"
