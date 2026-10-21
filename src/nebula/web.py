"""HTTP routes beside MCP: health, signed file transfer, and simple read-only pages."""

from __future__ import annotations

import html
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from nebula import access, db, files, limits, storage
from nebula.server import mcp


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(request: Request) -> Response:
    try:
        await db.fetch_one("select 1 as ok")
        return JSONResponse({"ok": True})
    except Exception as e:  # pragma: no cover
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@mcp.custom_route("/files/{token}", methods=["GET", "PUT"], include_in_schema=False)
async def file_transfer(request: Request) -> Response:
    payload = storage.verify_token(request.path_params["token"])
    if not payload or payload.get("m") != request.method:
        return PlainTextResponse("This link is invalid or has expired. Ask Nebula for a new one.", status_code=403)
    if payload.get("p"):
        row = await db.fetch_one("select email,status from people where id=%s", (payload["p"],))
        if not row or row["status"] != "active":
            return PlainTextResponse("This account no longer has access.", status_code=403)
        person = await access.get_person(row["email"])
        if payload.get("r"):
            kind, rid = payload["r"]
            if not access.at_least(await access.level_for(person, kind, rid), "view" if request.method == "GET" else "edit"):
                return PlainTextResponse("Access was revoked. Ask Nebula for a new link.", status_code=403)
    key = payload["k"]
    if request.method == "GET":
        if await storage.exists(key) is None:
            return PlainTextResponse("Not found.", status_code=404)
        data = await storage.get(key)
        name = payload.get("f") or key.rsplit("/", 1)[-1]
        return Response(
            data,
            media_type=payload.get("t") or "application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
        )
    from nebula.config import settings
    max_bytes = min(int(payload.get("max", settings().max_upload_bytes)), settings().max_upload_bytes)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            return PlainTextResponse(f"File too large (limit {max_bytes} bytes).", status_code=413)
    if not body:
        return PlainTextResponse("Empty upload. Send the file with: curl -X PUT --data-binary @file '<url>'",
                                 status_code=400)
    upload_id = payload.get("u")
    async with db.tx() as conn:
        if upload_id:
            # Lock the upload row so one link stores at most one file, even when PUTs race or are replayed.
            up = await db.fetch_one("select status from uploads where id=%s for update", (upload_id,), conn)
            if not up or up["status"] != "pending":
                return PlainTextResponse("This upload link was already used. Start a new upload to send another file.",
                                         status_code=409)
        try:
            await storage.put(key, bytes(body), request.headers.get("content-type", ""), person_id=payload.get("p"))
        except limits.LimitExceeded as exc:
            return PlainTextResponse(str(exc), status_code=413)
        job = await files.mark_received(upload_id, conn) if upload_id else None
    return JSONResponse({"stored": True, "bytes": len(body), "indexing_job": job})


# Pages and records open in the web viewer (webui.py), which checks sign-in and access.

_STYLE = """<style>:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a64;--accent:#3d5afe}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#a3a29b;--accent:#8c9eff}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 16px}h1{font-size:1.6rem;margin:0 0 4px}
.meta{color:var(--muted);font-size:.9rem;margin-bottom:24px}a{color:var(--accent)}code{overflow-wrap:anywhere}</style>"""


@mcp.custom_route("/page/{rid}", methods=["GET"], include_in_schema=False)
async def view_page(request: Request) -> Response:
    from nebula import webui

    return await webui.page_view(request, request.path_params["rid"])


@mcp.custom_route("/record/{rid}", methods=["GET"], include_in_schema=False)
async def view_record(request: Request) -> Response:
    from nebula import webui

    return await webui.record_view(request, request.path_params["rid"])


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def home(request: Request) -> Response:
    from nebula.config import settings

    url = settings().public_url.rstrip("/") + "/mcp"
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Nebula</title>{_STYLE}<main><h1>Nebula</h1><div class=meta>Your knowledge, skills and tools, used through "
        f"Claude.</div><h2>Connect</h2><ol><li>In Claude, open Customize → Connectors → Add custom connector.</li>"
        f"<li>Paste <code>{html.escape(url)}</code>, then sign in with Google.</li>"
        f"<li>Settings → Capabilities → Code execution: add <code>{html.escape(settings().public_url.split('//')[-1])}"
        f"</code> to the allowed domains, so Claude can move files in and out.</li></ol>"
        f"<p>Ask Claude to search the information you can access, cite sources and save what you decide. "
        f"Signing in does not give you access to someone else's knowledge.</p>"
        f"<p><a href='/web'>Browse Nebula</a>{' · ' + settings().footer_links() if settings().footer_links() else ''}"
        f"</p></main>"
    )
