"""The permission gaps found while fact-checking the explainer, and the limits that follow each action."""

from __future__ import annotations

from helpers import call, drain
from nebula import access, db, org, sources
from nebula.config import settings
from nebula.connectors.base import FetchedItem
from nebula.knowledge import items

ADMIN = "admin@example.org"
SHARER = "gap-sharer@example.org"
READER = "gap-reader@example.org"
TEAM_LEAD = "gap-lead@example.org"
TEAMMATE = "gap-teammate@example.org"
STRANGER = "gap-stranger@example.org"


async def _drive_file(external_id: str, title: str, audience: dict) -> str:
    space = await sources.drive_space()
    fetched = FetchedItem(external_id=external_id, title=title, url="", mime_type="text/plain", kind="file",
                          data=f"{title}: confidential numbers.".encode(), filename=f"{external_id}.txt")
    async with db.tx() as conn:
        res = await items.upsert(conn, str(space["id"]), fetched)
        await items.apply_source_audience(conn, space, res["id"], audience)
    return res["id"]


def _audience(emails=(), groups=()) -> dict:
    return {"emails": list(emails), "groups": list(groups), "domains": [], "public": False, "unknown": False}


async def test_managing_a_mirrored_collection_does_not_list_its_titles(mcp):
    """Whoever connected a Drive folder manages it, but only sees the files Drive shares with them."""
    from starlette.testclient import TestClient

    from nebula import server

    sharer = await access.ensure_person(SHARER)
    await access.ensure_person(READER)
    col = await db.fetch_one(
        """insert into collections (slug, name, kind, source_config, owner_person)
           values ('gap-folder', 'Gap folder', 'gdrive', %s, %s) returning *""",
        (db.jsonb({"folder_id": "gapfolder01", "permissions": "mirror"}), sharer.id))
    fetched = FetchedItem(external_id="gap-secret-file", title="Board salaries", url="", mime_type="text/plain",
                          kind="file", data=b"Board salaries: confidential numbers.", filename="salaries.txt")
    async with db.tx() as conn:
        res = await items.upsert(conn, str(col["id"]), fetched)
        await items.apply_source_audience(conn, col, res["id"], _audience([READER]))
    assert await access.level_for(sharer, "collection", str(col["id"])) == "manage"
    assert res["id"] not in await access.visible_items(sharer, str(col["id"]))

    recent = await org.recent_changes(await access.get_person(SHARER), 7, "gap-folder")
    assert not any(r["title"] == "Board salaries" for r in recent["sources"])
    recent = await org.recent_changes(await access.get_person(READER), 7)
    assert any(r["title"] == "Board salaries" for r in recent["sources"])

    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    settings().dev_user_email = SHARER
    server.forget_person()
    with TestClient(app) as c:
        page = c.get("/web/c/gap-folder")
        assert page.status_code == 200 and "Gap folder" in page.text and "Board salaries" not in page.text
    settings().dev_user_email = READER
    server.forget_person()
    with TestClient(app) as c:
        assert "Board salaries" in c.get("/web").text  # listed under "Shared with you"


async def test_a_lead_hands_over_only_their_own_team(mcp):
    admin = await access.ensure_person(ADMIN)
    for who in (TEAM_LEAD, TEAMMATE, STRANGER):
        await access.ensure_person(who)
    await org.create_group(admin, "gapteam", "Gap team", lead_email=TEAM_LEAD)
    await org.set_member(admin, "gapteam", TEAMMATE)
    r = await call(mcp, TEAMMATE, "save", kind="page", title="Gap team rota", body="Who's on duty.",
                   collection="gapteam-notebook", confirm=True)
    page_id = r["id"].split(":", 1)[1]
    await db.execute("update pages set owner_person=(select id from people where email=%s) where id=%s",
                     (TEAMMATE, page_id))
    elsewhere = await db.fetch_one(
        """insert into collections (slug, name, kind, owner_person) values ('gap-elsewhere', 'Elsewhere', 'notebook',
           (select id from people where email=%s)) returning id""", (TEAMMATE,))
    mine = await db.fetch_one(
        """insert into pages (collection_id, slug, title, body, owner_person)
           values (%s, 'my-own-notes', 'My own notes', 'Private.', (select id from people where email=%s)) returning id""",
        (elsewhere["id"], TEAMMATE))

    r = await call(mcp, TEAM_LEAD, "admin", action="handover", from_email=STRANGER, to_email=TEAM_LEAD, group="gapteam")
    assert "isn't in 'gapteam'" in r["error"]
    r = await call(mcp, TEAM_LEAD, "admin", action="handover", from_email=TEAMMATE, to_email=TEAM_LEAD,
                   group="gapteam", leaving=True)
    assert "Only an admin" in r["error"]
    r = await call(mcp, TEAM_LEAD, "admin", action="handover", from_email=TEAMMATE, to_email=TEAM_LEAD,
                   group="gapteam", apply=True)
    assert r["done"] and r["summary"]["personally_owned"] == {"page": ["Gap team rota"]}
    owner = await db.fetch_one("select p.email from pages g join people p on p.id=g.owner_person where g.id=%s", (page_id,))
    assert owner["email"] == TEAM_LEAD
    still = await db.fetch_one("select p.email from pages g join people p on p.id=g.owner_person where g.id=%s",
                               (mine["id"],))
    assert still["email"] == TEAMMATE  # work outside the team isn't the lead's to move


async def test_linking_a_google_group_opens_files_shared_with_it(mcp):
    admin = await access.ensure_person(ADMIN)
    await org.create_group(admin, "gapgroup", "Gap group")
    await org.set_member(admin, "gapgroup", TEAMMATE)
    item_id = await _drive_file("gap-group-file", "Group budget", _audience(groups=["gap-team@example.org"]))
    teammate = await access.get_person(TEAMMATE)
    assert await access.level_for(teammate, "item", item_id) == "none"
    r = await call(mcp, ADMIN, "sources", action="status", id=f"item:{item_id}")
    assert "gap-team@example.org" in r["sharing_note"]
    r = await call(mcp, ADMIN, "admin", action="set_group_email", group="gapgroup",
                   google_group="Gap-Team@example.org")
    assert r["files_now_visible_to_the_team"] == 1
    assert await access.level_for(await access.get_person(TEAMMATE), "item", item_id) == "view"
    await drain()


async def test_page_owners_decide_proposals_without_admin(mcp):
    owner, other = "gap-page-owner@example.org", "gap-page-editor@example.org"
    admin = await access.ensure_person(ADMIN)
    await org.create_group(admin, "gapdocs", "Gap docs")
    for who in (owner, other):
        await access.ensure_person(who)
        await org.set_member(admin, "gapdocs", who)
    p = await call(mcp, owner, "save", kind="page", title="Gap field guide", body="Version one.",
                   collection="gapdocs-notebook", confirm=True)
    await db.execute("update pages set owner_person=(select id from people where email=%s), status='verified' where id=%s",
                     (owner, p["id"].split(":", 1)[1]))
    r = await call(mcp, other, "save", kind="page", id=p["id"], expected_version=1, body="Version two, with a map.")
    assert r["proposed"]
    seen = await call(mcp, owner, "open", id=p["id"])
    [prop] = seen["proposals_to_decide"]
    assert prop["by"] == other
    r = await call(mcp, other, "save", kind="page", proposal_id=prop["proposal_id"], accept=True)
    assert "error" in r  # only someone who manages the page decides
    r = await call(mcp, owner, "save", kind="page", proposal_id=prop["proposal_id"], accept=True)
    assert r["status"] == "accepted"
    seen = await call(mcp, owner, "open", id=p["id"])
    assert "Version two" in seen["_body"] and "proposals_to_decide" not in seen


async def test_reading_does_not_use_up_the_write_limit(mcp, monkeypatch):
    who = "gap-reader-only@example.org"
    await call(mcp, who, "people", groups=True)
    monkeypatch.setattr(settings(), "writes_per_day", 1)
    for _ in range(3):
        assert "error" not in await call(mcp, who, "skills", action="find", task="monthly report")
        assert "error" not in await call(mcp, who, "sources", action="list")
    r = await call(mcp, who, "save", kind="record", type="fact", title="Gap fact", body="x", confirm=True)
    assert r["saved"]
    r = await call(mcp, who, "save", kind="record", type="fact", title="Gap fact two", body="y", confirm=True)
    assert "writes limit is reached" in r["error"]


def test_long_results_are_cut_with_a_note(monkeypatch):
    from fastmcp.tools.base import ToolResult
    from mcp_types import TextContent

    from nebula import server

    monkeypatch.setattr(settings(), "max_result_chars", 100)
    short = ToolResult(content=[TextContent(type="text", text="ok")])
    assert server._bounded(short) is short
    cut = server._bounded(ToolResult(content=[TextContent(type="text", text="x" * 500)]))
    assert cut.content[0].text.startswith("x" * 100) and "cut this result at 100" in cut.content[0].text
