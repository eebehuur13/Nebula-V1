"""Assemble the HTTP app: MCP endpoint at /mcp, Google sign-in, routes, startup work."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from nebula import db, models
from nebula.config import settings

log = logging.getLogger(__name__)


def build_app():
    from nebula import server, web, webui  # noqa: F401  (they register routes on the server)

    s = settings()
    if s.auth_mode == "google":
        server.mcp.auth = _google_auth()
    server.load_gap_tools()

    # No Host allowlist: that guard protects servers bound to localhost from DNS rebinding.
    # Nebula sits behind CloudFront (origin secret header), and the load balancer's health
    # checks arrive with the task's IP as Host, which an allowlist would reject.
    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        applied = await db.migrate()
        if applied:
            log.info("migrations applied: %s", applied)
        await db.pool()
        from nebula import skills

        seeded = await skills.seed()
        if seeded:
            log.info("starter skills installed/updated: %s", seeded)
        asyncio.get_running_loop().run_in_executor(None, models.warm)
        async with inner_lifespan(a):
            yield
        await db.close()

    app.router.lifespan_context = lifespan
    return app


def _google_auth():
    from fastmcp.server.auth.providers.google import GoogleProvider
    from key_value.aio.stores.postgresql import PostgreSQLStore

    s = settings()
    storage = PostgreSQLStore(url=s.dsn, table_name="oauth_kv")
    if not s.google_client_id:
        # Until the Google OAuth client is set in the secret, sign-in can't complete, so no one
        # can get a token. The service still starts and /healthz works.
        log.warning("NEBULA_GOOGLE_CLIENT_ID is not set: sign-in will fail until it is configured")
    return GoogleProvider(
        client_id=s.google_client_id or "not-configured.apps.googleusercontent.com",
        client_secret=s.google_client_secret or "not-configured",
        base_url=s.public_url,
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
        client_storage=storage,
        jwt_signing_key=s.jwt_signing_key,
        cache_ttl_seconds=300,
        require_authorization_consent="remember",  # one click per client app, then remembered
        extra_authorize_params={"access_type": "offline", "prompt": "consent",
                                **({"hd": next(iter(s.allowed_domains))} if len(s.allowed_domains) == 1
                                   and not s.allowed_email_set and not s.public_signup else {})},
        allowed_client_redirect_uris=["https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback",
                                      "http://localhost:*", "http://127.0.0.1:*"],
    )
