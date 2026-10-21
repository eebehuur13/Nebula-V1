"""Sign-in is not a content permission; browser callbacks belong to one browser."""
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.requests import Request

from nebula import access, webui
from nebula.config import Settings, settings


def request(path, query="", cookie=""):
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode(),
                    "headers": headers, "scheme": "https", "server": ("example.test", 443)})


def test_public_identity_does_not_grant_rl_membership():
    cfg = Settings(public_signup=True)
    assert cfg.email_allowed("someone@example.com")
    assert not cfg.email_allowed("@example.com")
    assert not cfg.email_allowed("")
    assert not Settings(public_signup=False).email_allowed("someone@example.com")
    outsider = access.Person("guest", "someone@example.com", "", False)
    member = access.Person("member", "someone@example.org", "", False)
    invited = access.Person("invited", "someone@example.com", "", False, {"all_rl": "member"})
    assert outsider.principals() == [("person", "guest")]
    assert outsider.group_level("all_rl") == "none"
    assert member.group_level("all_rl") == invited.group_level("all_rl") == "view"


@pytest.mark.parametrize("destination", ["//evil.example", "https://evil.example", "/\\evil.example", "/web"])
async def test_public_login_requests_identity_and_safe_redirect(monkeypatch, destination):
    from urllib.parse import urlencode

    monkeypatch.setattr(settings(), "public_signup", True)
    response = await webui.login(request("/login", urlencode({"next": destination})))
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert set(params["scope"][0].split()) == {"openid", "email", "profile"}
    assert "hd" not in params
    assert webui._unsign(params["state"][0])["next"] == "/web"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]


async def test_browser_callback_rejects_state_from_another_browser():
    state = webui._sign({"next": "/web", "exp": time.time() + 60})
    response = await webui.callback(request("/web/callback", f"code=unused&state={state}"))
    assert b"expired" in response.body


def test_gap_tools_do_not_make_all_rl_public():
    from types import SimpleNamespace
    from nebula import server

    outsider = access.Person("guest", "someone@example.com", "", False)
    previous = server._gap_tools.copy()
    try:
        server._gap_tools["private-extension"] = SimpleNamespace(groups=["all_rl"])
        assert not server._visible_to(outsider, "private-extension")
        assert not server._visible_to(outsider, "admin")
    finally:
        server._gap_tools.clear()
        server._gap_tools.update(previous)


@pytest.mark.parametrize("verified", [False, "false", None])
async def test_mcp_requires_verified_google_identity(monkeypatch, verified):
    from types import SimpleNamespace
    from fastmcp.exceptions import ToolError
    from nebula import server

    monkeypatch.setattr(settings(), "auth_mode", "google")
    monkeypatch.setattr(server, "get_access_token", lambda: SimpleNamespace(
        claims={"email": "someone@example.com", "email_verified": verified}))
    with pytest.raises(ToolError, match="verified Google email"):
        await server.me()


async def test_browser_callback_accepts_bound_verified_identity(monkeypatch, respx_mock):
    import httpx
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings(), "public_signup", True)
    ensure = AsyncMock()
    monkeypatch.setattr(access, "ensure_person", ensure)
    respx_mock.post("https://oauth2.googleapis.com/token").mock(return_value=httpx.Response(200, json={"access_token": "fake"}))
    respx_mock.get("https://openidconnect.googleapis.com/v1/userinfo").mock(return_value=httpx.Response(200, json={
        "email": "someone@example.com", "email_verified": True, "sub": "google-user", "name": "Someone"}))
    state = webui._sign({"next": "/web", "exp": time.time() + 60})
    response = await webui.callback(request("/web/callback", f"code=fake&state={state}",
                                           f"{webui.STATE_COOKIE}={state}"))
    assert response.status_code == 302 and response.headers["location"] == "/web"
    ensure.assert_awaited_once_with("someone@example.com", "Someone", "google-user")
    cookies = response.headers.getlist("set-cookie")
    assert any(webui.STATE_COOKIE in c and "Max-Age=0" in c for c in cookies)
    assert any(webui.COOKIE + "=" in c and "HttpOnly" in c for c in cookies)
