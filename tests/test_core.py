"""End-to-end tests of the core through the MCP tools, against a real local Postgres.

Needs the local pgvector container (see README) with a `nebula_test` database.
"""

from __future__ import annotations

import io

import pytest
from fastmcp import Client

from helpers import ADMIN, LEAD, MEMBER, NEWLEAD, OUTSIDER, call, drain  # noqa: F401
from nebula import db, jobs, storage
from nebula.config import settings


async def test_people_groups_and_notebooks(mcp):
    for who in (ADMIN, LEAD, MEMBER, OUTSIDER, NEWLEAD):
        await call(mcp, who, "people", groups=True)  # first call creates the person
    r = await call(mcp, ADMIN, "admin", action="create_group", group="ap", name="Andhra Pradesh", email=LEAD)
    assert r["notebook"] == "ap-notebook"
    r = await call(mcp, LEAD, "admin", action="add_member", group="ap", email=MEMBER)
    assert r["role"] == "member"
    # admin tool is hidden from plain members
    from nebula import server
    settings().dev_user_email = MEMBER
    server.forget_person()
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert "admin" not in names and {"search", "open", "save", "skills", "file", "query"} <= names


async def test_save_search_open_and_permissions(mcp):
    page = await call(mcp, LEAD, "save", kind="page", title="Andhra Pradesh programme", page_kind="programme",
                      aliases=["AP"], body="# Overview\nTeacher training happens in Guntur and Krishna.\n\n"
                                          "See [[Teacher training schedule]].", confirm=True)
    assert page["saved"] and page["unresolved_links"] == ["Teacher training schedule"]
    sched = await call(mcp, MEMBER, "save", kind="page", title="Teacher training schedule", page_kind="process",
                       body="Training runs every second Saturday from January.", confirm=True)
    assert sched["saved"]
    dec = await call(mcp, MEMBER, "save", kind="record", type="decision",
                     title="Move AP teacher training to Saturdays",
                     body="Decided in the October review because weekday attendance was low.",
                     evidence=[sched["id"]], valid_from="2026-10-01", confirm=True)
    assert dec["saved"] and dec["status"] == "draft"
    await drain()

    r = await call(mcp, MEMBER, "search", query="when is teacher training in AP")
    ids = [x["id"] for x in r["results"]]
    assert dec["id"] in ids and page["id"] in ids
    # The outsider (not in the AP group) sees nothing from the AP notebook
    r = await call(mcp, OUTSIDER, "search", query="teacher training")
    assert not r["results"]
    r = await call(mcp, OUTSIDER, "open", id=page["id"])
    assert "error" in r

    # Similar-item check before saving a duplicate
    r = await call(mcp, MEMBER, "save", kind="record", type="decision",
                   title="Move AP teacher training to Saturdays", body="weekday attendance was low")
    assert r["saved"] is False and r["similar"]

    # Page links resolve once the target exists, and open shows them
    r = await call(mcp, LEAD, "save", kind="page", id=page["id"], expected_version=1,
                   title="Andhra Pradesh programme", body="Teacher training: see [[Teacher training schedule]].")
    assert r["version"] == 2 and "unresolved_links" not in r
    r = await call(mcp, MEMBER, "open", id=sched["id"])
    assert any(n["id"] == page["id"] for n in r["linked_from"])
    # Stale version is refused
    r = await call(mcp, LEAD, "save", kind="page", id=page["id"], expected_version=1, title="x", body="y")
    assert "changed since you read it" in r["error"]

    # Sharing one page with the outsider makes just that page visible
    r = await call(mcp, LEAD, "share", id=page["id"], add=[{"who": OUTSIDER, "level": "view"}])
    assert any(s["who"] == OUTSIDER for s in r["access"]["shared_with"])
    r = await call(mcp, OUTSIDER, "open", id=page["id"])
    assert r["title"] == "Andhra Pradesh programme"
    r = await call(mcp, OUTSIDER, "open", id=sched["id"])
    assert "error" in r
    # Members can't share with All RL
    r = await call(mcp, MEMBER, "share", id=sched["id"], add=[{"who": "all_rl"}])
    assert "error" in r


async def test_verified_pages_take_proposals(mcp):
    p = await call(mcp, LEAD, "save", kind="page", title="AP partners", page_kind="partner",
                   body="Partner list.", status="verified", confirm=True)
    assert p["status"] == "verified"
    r = await call(mcp, MEMBER, "save", kind="page", id=p["id"], expected_version=1, title="AP partners",
                   body="Partner list, plus the new district partner.")
    assert r.get("proposed")
    q = await call(mcp, LEAD, "admin", action="review")
    prop = q["open_proposals"][0]
    r = await call(mcp, LEAD, "admin", action="decide_proposal", proposal_id=prop["proposal_id"], accept=True)
    assert r["status"] == "accepted"


async def test_corrections_supersede(mcp):
    f = await call(mcp, MEMBER, "save", kind="record", type="fact", title="AP has 1,200 active centres",
                   body="As of Q1.", confirm=True)
    c = await call(mcp, MEMBER, "save", kind="record", type="correction", title="AP has 1,350 active centres",
                   body="The Q1 figure was wrong.", supersedes=f["id"], confirm=True)
    await drain()
    r = await call(mcp, MEMBER, "search", query="AP active centres", types=["record"])
    ids = [x["id"] for x in r["results"]]
    assert c["id"] in ids and f["id"] not in ids
    r = await call(mcp, MEMBER, "open", id=f["id"])
    assert r["status"] == "superseded" and r["superseded_by"] == c["id"]


async def test_upload_spreadsheet_and_query(mcp):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Centres"
    ws.append(["District", "Month", "Active centres", "Children enrolled"])
    rows = [("Guntur", "2026-07-01", 410, 9800), ("Guntur", "2026-08-01", 425, 10100),
            ("Krishna", "2026-07-01", 380, 8700), ("Krishna", "2026-08-01", 390, 9050)]
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)

    up = await call(mcp, MEMBER, "file", action="upload", filename="ap_centres.xlsx", title="AP centres by month")
    assert up["collection"] == "Andhra Pradesh notebook"
    # Simulate the sandbox's curl PUT
    token = up["url"].rsplit("/files/", 1)[1]
    payload = storage.verify_token(token)
    assert payload and payload["m"] == "PUT"
    await storage.put(payload["k"], buf.getvalue(), "")
    from nebula import files
    await files.mark_received(payload["u"])
    await drain()
    st = await call(mcp, MEMBER, "file", action="status", upload_id=up["upload_id"])
    assert st["status"] == "done" and st["item"].startswith("item:")

    r = await call(mcp, MEMBER, "search", query="AP centres by month active centres", types=["dataset"])
    ds = r["results"][0]
    assert ds["type"] == "dataset"
    d = await call(mcp, MEMBER, "open", id=ds["id"])
    cols = {c["name"] for c in d["columns"]}
    assert "active_centres" in cols
    q = await call(mcp, MEMBER, "query",
                   sql=f"select district, sum(active_centres) as total from {d['table']} group by district order by district")
    assert q["rows"] == [["Guntur", 835], ["Krishna", 770]]
    # Outsider can't query it, and nobody can read Nebula's own tables
    r = await call(mcp, OUTSIDER, "query", sql=f"select * from {d['table']}")
    assert "error" in r
    r = await call(mcp, MEMBER, "query", sql="select * from people")
    assert "error" in r
    r = await call(mcp, MEMBER, "query", sql="delete from people")
    assert "error" in r

    # Download returns a signed link on Nebula's own domain
    dl = await call(mcp, MEMBER, "file", action="download", id=st["item"])
    assert dl["url"].startswith("http://testserver/files/")


async def test_search_returns_short_files_whole(mcp):
    """A short file comes back complete; a long one comes back as excerpts with a note to open it."""
    from nebula import files

    story = "Asha built a small robot called Orbit. " * 60  # ~2,500 characters, like the Drive test file
    survey = " ".join(f"Paragraph {i}: the zebra survey counted herds near river bend {i}." for i in range(200))
    ids = {}
    for filename, title, text in (("story.txt", "Robot story", story), ("survey.txt", "Zebra survey", survey)):
        up = await call(mcp, MEMBER, "file", action="upload", filename=filename, title=title)
        payload = storage.verify_token(up["url"].rsplit("/files/", 1)[1])
        await storage.put(payload["k"], text.encode(), "")
        await files.mark_received(payload["u"])
        await drain()
        ids[title] = (await call(mcp, MEMBER, "file", action="status", upload_id=up["upload_id"]))["item"]

    r = await call(mcp, MEMBER, "search", query="robot Orbit story", types=["item"])
    hit = next(x for x in r["results"] if x["id"] == ids["Robot story"])
    assert hit["complete"] is True and "matches" not in hit
    assert hit["content"].count("Orbit") == 60

    r = await call(mcp, MEMBER, "search", query="zebra survey river bend", types=["item"])
    hit = next(x for x in r["results"] if x["id"] == ids["Zebra survey"])
    assert hit["matches"] and "complete" not in hit
    assert "Open an id" in r["note"]


async def test_skills_lifecycle(mcp):
    md = ("---\nname: ap-monthly-update\ndescription: Write the AP monthly programme update.\n---\n"
          "1. Search Nebula for this month's AP decisions.\n2. Use the template file.\n")
    r = await call(mcp, MEMBER, "skills", action="save", name="ap-monthly-update", skill_md=md,
                   files={"template.md": "# AP update for {month}"}, owner="ap")
    assert r["status"] == "draft"
    r = await call(mcp, OUTSIDER, "skills", action="find", task="monthly update")
    assert not any(s["name"] == "ap-monthly-update" for s in r["skills"])
    r = await call(mcp, MEMBER, "skills", action="publish", name="ap-monthly-update")
    assert "error" in r  # members can't publish
    r = await call(mcp, LEAD, "skills", action="publish", name="ap-monthly-update")
    assert r["status"] == "published"
    r = await call(mcp, MEMBER, "skills", action="get", name="ap-monthly-update")
    assert r["files"]["template.md"]["text"].startswith("# AP update")
    r = await call(mcp, LEAD, "skills", action="publish", name="ap-monthly-update", share_with=["all_rl"])
    r = await call(mcp, OUTSIDER, "skills", action="find", task="monthly update")
    assert any(s["name"] == "ap-monthly-update" for s in r["skills"])


async def test_handover(mcp):
    r = await call(mcp, ADMIN, "admin", action="handover", from_email=LEAD, to_email=NEWLEAD, group="ap")
    assert "ap" in r["preview"]["lead_of"]
    r = await call(mcp, ADMIN, "admin", action="handover", from_email=LEAD, to_email=NEWLEAD, group="ap", apply=True)
    assert r["done"]
    r = await call(mcp, NEWLEAD, "admin", action="review")
    assert "stale_pages" in r
    r = await call(mcp, NEWLEAD, "search", query="teacher training")
    assert r["results"]
    r = await call(mcp, LEAD, "search", query="teacher training")
    assert not r["results"]  # the old lead lost AP access


async def test_seed_skills_recent_and_export(mcp):
    from nebula import skills

    assert set(await skills.seed()) >= {"nebula-pages", "nebula-upkeep"}
    assert await skills.seed() == []  # idempotent
    r = await call(mcp, OUTSIDER, "skills", action="find", task="upkeep stale pages weekly")
    assert r["skills"][0]["name"] == "nebula-upkeep"
    r = await call(mcp, NEWLEAD, "admin", action="recent", days=7)
    assert r["pages"] and r["records"]
    r = await call(mcp, NEWLEAD, "admin", action="export", collection="ap-notebook")
    assert r["pages"] >= 3 and r["url"].startswith("http://testserver/files/")


async def test_web_viewer(mcp):
    from starlette.testclient import TestClient

    from nebula import server

    settings().dev_user_email = MEMBER
    server.forget_person()
    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    page = await db.fetch_one("select id from pages where title='Teacher training schedule'")
    with TestClient(app) as c:
        r = c.get("/web")
        assert r.status_code == 200 and "Andhra Pradesh notebook" in r.text
        r = c.get(f"/page/{page['id']}")
        assert r.status_code == 200 and "Linked from" in r.text and "Andhra Pradesh programme" in r.text
        g = c.get("/web/c/ap-notebook/graph.json").json()
        assert g["nodes"] and g["links"]
    settings().dev_user_email = OUTSIDER
    server.forget_person()
    with TestClient(app) as c:
        r = c.get(f"/page/{page['id']}")
        assert "Not found" in r.text


async def test_public_signin_isolated_notebooks_and_explicit_sharing(mcp, monkeypatch):
    from nebula import access, server

    monkeypatch.setattr(settings(), "public_signup", True)
    guest = "same-person@example.com"
    second = "same-person@example.net"
    created = await call(mcp, ADMIN, "admin", action="create_collection", slug="rl-signup-test",
                         name="RL-only knowledge", source="notebook", group="all_rl")
    assert "error" not in created
    rl_page = await call(mcp, ADMIN, "save", kind="page", title="RL internal signup evidence",
                         body="Organisation information for RL members only.", collection="rl-signup-test",
                         confirm=True)
    assert rl_page["saved"]

    first = await call(mcp, guest, "save", kind="page", title="Guest private evidence",
                       body="Private notes from the first account.", confirm=True)
    other = await call(mcp, second, "save", kind="page", title="Second private evidence",
                       body="Private notes from the second account.", confirm=True)
    assert first["saved"] and other["saved"]
    one = await access.get_person(guest)
    two = await access.get_person(second)
    assert one and two and not one.is_rl_member and not two.is_rl_member
    assert ("group", "all_rl") not in one.principals()
    assert one.group_level("all_rl") == "none"
    rows = await db.fetch_all("select owner_person, slug from collections where owner_person = any(%s::uuid[])",
                              ([one.id, two.id],))
    assert len(rows) == 2 and len({r["slug"] for r in rows}) == 2
    await call(mcp, ADMIN, "save", kind="page", id=rl_page["id"], expected_version=1,
               title="RL internal signup evidence", body="Private backlink to [[Guest private evidence]].")
    opened = await call(mcp, guest, "open", id=first["id"])
    assert not opened["linked_from"]  # a shared target must not disclose private backlinks
    unresolved = await call(mcp, guest, "save", kind="page", title="Guest link test",
                            body="[[RL internal signup evidence]]", confirm=True)
    assert unresolved["unresolved_links"] == ["RL internal signup evidence"]
    assert "error" in await call(mcp, guest, "open", id=rl_page["id"])
    assert "error" in await call(mcp, second, "open", id=first["id"])
    results = (await call(mcp, guest, "search", query="RL internal signup evidence"))["results"]
    assert rl_page["id"] not in {r["id"] for r in results}
    assert "error" in await call(mcp, guest, "skills", action="get", name="nebula-pages")
    assert (await call(mcp, guest, "people", groups=True))["groups"] == []
    assert not (await call(mcp, guest, "people", query="example.org"))["people"]
    assert "error" in await call(mcp, guest, "people", email=ADMIN)
    assert not (await call(mcp, MEMBER, "people", query=guest))["people"]
    system_job = await jobs.enqueue("internal-only", {})
    assert "error" in await call(mcp, guest, "job", id=system_job)
    dataset = await db.fetch_one("select table_name from datasets limit 1")
    assert "error" in await call(mcp, guest, "query", sql=f"select * from data.{dataset['table_name']}")
    settings().dev_user_email = guest
    server.forget_person()
    async with Client(mcp) as c:
        assert "admin" not in {t.name for t in await c.list_tools()}

    shared = await call(mcp, guest, "share", id=first["id"], add=[{"who": second, "level": "view"}])
    assert "error" not in shared
    assert (await call(mcp, second, "open", id=first["id"]))["title"] == "Guest private evidence"
    assert "error" in await call(mcp, second, "save", kind="page", id=first["id"], expected_version=1,
                                 title="Guest private evidence", body="Cannot edit with view access.")
    await call(mcp, guest, "share", id=first["id"], add=[{"who": second, "level": "edit"}])
    edited = await call(mcp, second, "save", kind="page", id=first["id"], expected_version=1,
                        title="Guest private evidence", body="An explicitly authorised edit.")
    assert edited["version"] == 2
    await call(mcp, guest, "share", id=first["id"], remove=[second])
    assert "error" in await call(mcp, second, "open", id=first["id"])
    assert "error" in await call(mcp, guest, "share", id=first["id"], add=[{"who": "all_rl"}])

    # RL-wide access is an explicit admin decision for an outside account.
    await call(mcp, ADMIN, "admin", action="add_member", group="all_rl", email=guest)
    assert (await access.get_person(guest)).is_rl_member
    assert (await call(mcp, guest, "open", id=rl_page["id"]))["title"] == "RL internal signup evidence"
    # All RL is read-only, so default saves must still use the guest's own notebook.
    saved = await call(mcp, guest, "save", kind="page", title="Still a personal note", body="My own note.", confirm=True)
    assert saved["saved"]
    await call(mcp, ADMIN, "admin", action="remove_member", group="all_rl", email=guest)
    assert "error" in await call(mcp, guest, "open", id=rl_page["id"])


async def test_drive_sharing_is_mirrored(mcp, monkeypatch):
    """Drive sharing decides who sees each file; sharing a folder with Nebula doesn't reveal it to the sharer."""
    from nebula import connectors, sources
    from nebula.connectors import gdrive
    from nebula.connectors.base import FetchedItem
    from nebula.knowledge import items

    folder = "1ctsTrdHykTIv-phcl6rwvHPYQbu1_kGl"

    class FakeConnector:
        def __init__(self, config):
            self.config = config

        async def describe(self, file_id):
            return {"id": file_id, "name": "Nebula Test", "mimeType": gdrive.FOLDER}

        async def covering_root(self, meta, cursor=None):
            return None

        async def close(self):
            return None

    monkeypatch.setitem(connectors.CONNECTORS, "gdrive", FakeConnector)
    monkeypatch.setattr(gdrive, "service_account_email", lambda: "nebula-drive@example.iam.gserviceaccount.com")
    r = await call(mcp, OUTSIDER, "sources", action="connect", link=f"https://drive.google.com/drive/folders/{folder}")
    source = r["source"]
    assert r["kind"] == "folder" and r["name"] == "Nebula Test" and r["job"] and r["estimate"]
    again = await call(mcp, OUTSIDER, "sources", action="connect", link=folder)
    assert again["already_connected"] and again["source"] == source
    space = await sources.drive_space()

    # This permission test seeds the stored item directly instead of calling real Drive.
    await db.execute("update jobs set status='done' where kind='sync' and status='queued'")
    story = FetchedItem(external_id="f-story", title="Field Story", url="https://drive.google.com/f-story",
                        mime_type="text/plain", kind="file", data=b"Meera grew up in Guntur and loves robots.",
                        filename="Field Story.txt")
    async with db.tx() as conn:
        res = await items.upsert(conn, str(space["id"]), story)
        await conn.execute("update items set source_root=%s where id=%s", (folder, res["id"]))
        await items.apply_source_audience(conn, space, res["id"], {
            "emails": [MEMBER], "groups": [], "domains": [], "public": True, "unknown": False})
    await drain()

    r = await call(mcp, MEMBER, "search", query="Field story robots")
    assert any(x["id"] == f"item:{res['id']}" for x in r["results"])
    scoped = await call(mcp, MEMBER, "search", query="Field story robots", collection="drive")
    assert any(x["id"] == f"item:{res['id']}" for x in scoped["results"])
    # The person who shared the folder isn't on the file's Drive sharing, so they can't read it,
    # but they can still manage the source.
    r = await call(mcp, OUTSIDER, "search", query="Field story robots")
    assert not any(x["id"] == f"item:{res['id']}" for x in r["results"])
    r = await call(mcp, OUTSIDER, "open", id=f"item:{res['id']}")
    assert "error" in r
    r = await call(mcp, OUTSIDER, "sources", action="status", source=source)
    assert r["files"]["in_nebula"] == 1 and r["recent"] == []
    r = await call(mcp, MEMBER, "sources", action="status", source=source)
    assert "error" in r  # only whoever shared it (or an admin) manages it
    r = await call(mcp, MEMBER, "sources", action="status", id=f"item:{res['id']}")
    assert r["title"] == "Field Story" and r["sharing"].startswith("Follows Drive")
    r = await call(mcp, LEAD, "open", id=f"item:{res['id']}")
    assert "error" in r

    # A manual share made in Nebula survives the next sync; Drive changes replace mirrored ones.
    await db.execute("""insert into grants (resource_type, resource_id, principal_type, principal_id, level)
                        select 'item', %s, 'person', id::text, 'view' from people where email=%s""",
                     (res["id"], LEAD))
    async with db.tx() as conn:
        await items.apply_source_audience(conn, space, res["id"], {
            "emails": [OUTSIDER], "groups": [], "domains": [], "public": False, "unknown": False})
    r = await call(mcp, MEMBER, "open", id=f"item:{res['id']}")
    assert "error" in r  # removed in Drive -> removed here
    r = await call(mcp, OUTSIDER, "open", id=f"item:{res['id']}")
    assert r["title"] == "Field Story"
    r = await call(mcp, LEAD, "open", id=f"item:{res['id']}")
    assert r["title"] == "Field Story"  # the manual Nebula share is kept

    # The RL domain on Drive means All RL in Nebula
    async with db.tx() as conn:
        await items.apply_source_audience(conn, space, res["id"], {
            "emails": [], "groups": [], "domains": ["example.org"], "public": False, "unknown": False})
    r = await call(mcp, MEMBER, "open", id=f"item:{res['id']}")
    assert r["title"] == "Field Story"

    r = await call(mcp, OUTSIDER, "sources", action="list")
    assert [x["source"] for x in r["sources"]] == [source]
    r = await call(mcp, OUTSIDER, "sources", action="disconnect", source=source)
    assert r["status"] == "disconnected" and r["files_removed"] == 1
    await db.execute("update jobs set status='cancelled' where kind='sync' and status='queued'")
    r = await call(mcp, MEMBER, "search", query="Field story robots")
    assert not any(x["id"] == f"item:{res['id']}" for x in r["results"])
    r = await call(mcp, OUTSIDER, "sources", action="list")
    assert r["sources"] == []


async def test_drive_reads_only_what_changed(mcp):
    """Unchanged files aren't downloaded again, and sharing is re-read only when it may have changed."""
    from nebula import access, sync
    from nebula.connectors.base import Change, FetchedItem

    owner = await access.ensure_person("drive-refresh-owner@example.com")
    reader = await access.ensure_person("drive-refresh-reader@example.com")
    col = await db.fetch_one(
        """insert into collections(slug,name,kind,source_config,owner_person)
           values('drive-refresh-regression','Sharing refresh','gdrive',%s,%s) returning *""",
        (db.jsonb({"folder_id": "folder-refresh", "permissions": "mirror"}), owner.id),
    )
    audience = {"emails": [reader.email], "groups": [], "domains": [], "public": False, "unknown": False}

    class Connector:
        fetches = reads = 0
        sharing = audience

        async def fetch(self, ch):
            self.fetches += 1
            return FetchedItem(external_id="unchanged-file", title="ACL refresh regression", url="",
                               mime_type="text/plain", kind="file", data=b"Permission refresh regression test.",
                               filename="acl.txt", extra={"audience": self.sharing, "rev": ch.rev, "acl": ch.acl})

        async def file_audience(self, external_id):
            assert external_id == "unchanged-file"
            self.reads += 1
            return self.sharing

    def change(**kw):
        return Change(external_id="unchanged-file", title="ACL refresh regression", **{"rev": "r1", "acl": "p1,p2", **kw})

    c = Connector()
    outcome, item_id = await sync.process_change(col, c, change())
    assert outcome == "changed" and c.fetches == 1 and c.reads == 0
    assert await access.level_for(reader, "item", item_id) == "view"
    assert await access.level_for(owner, "item", item_id) == "none"
    # Same revision, same sharing: nothing downloaded or re-read.
    assert (await sync.process_change(col, c, change()))[0] == "unchanged"
    assert (c.fetches, c.reads) == (1, 0)
    # Sharing changed in Drive (new fingerprint): re-read, but the file isn't downloaded.
    c.sharing = {**audience, "emails": []}
    await sync.process_change(col, c, change(acl="p1"))
    assert (c.fetches, c.reads) == (1, 1)
    assert await access.level_for(reader, "item", item_id) == "none"
    # A folder above it was re-shared: re-read although its own fingerprint didn't change.
    c.sharing = audience
    await sync.process_change(col, c, change(acl="p1", recheck_acl=True))
    assert c.reads == 2 and await access.level_for(reader, "item", item_id) == "view"
    # No fingerprint from Drive: re-read on full listings only. The daily sweep re-reads everything.
    await sync.process_change(col, c, change(acl=None))
    assert c.reads == 2
    await sync.process_change(col, c, change(acl=None), full=True)
    assert c.reads == 3
    await sync.process_change(col, c, change(acl="p1"), sweep=True)
    assert c.reads == 4
    # New content: downloaded again.
    await sync.process_change(col, c, change(rev="r2", acl="p1"))
    assert c.fetches == 2
    # Unreadable sharing hides the file again (only mirrored grants are removed).
    c.sharing = {"emails": [], "groups": [], "domains": [], "public": False, "unknown": True}
    await sync.process_change(col, c, change(rev="r2", acl="p0"))
    assert await access.level_for(reader, "item", item_id) == "none"


async def test_storage_limits_are_atomic_and_owner_bound(mcp, monkeypatch):
    import asyncio
    from nebula import access, limits

    first = await access.ensure_person("quota-race-first@example.com")
    second = await access.ensure_person("quota-race-second@example.com")
    monkeypatch.setattr(settings(), "storage_bytes_per_person", 100)
    results = await asyncio.gather(limits.reserve_storage(first.id, "quota-race-shared", 30),
                                   limits.reserve_storage(second.id, "quota-race-shared", 30),
                                   return_exceptions=True)
    assert sum(isinstance(r, limits.LimitExceeded) for r in results) == 1
    total = await db.fetch_one("select sum(used_bytes) as n from storage_accounts where person_id=any(%s::uuid[])",
                               ([first.id, second.id],))
    assert total["n"] == 30
    owner = await db.fetch_one("select person_id from stored_objects where file_key='quota-race-shared'")
    await limits.reserve_storage(str(owner["person_id"]), "quota-race-shared", 100)
    with pytest.raises(limits.LimitExceeded):
        await limits.reserve_storage(str(owner["person_id"]), "quota-race-over", 1)
    await limits.release_storage("quota-race-shared")
    assert (await limits.usage(str(owner["person_id"])))["stored_bytes"] == 0

    monkeypatch.setattr(settings(), "tool_calls_per_minute", 5)
    results = await asyncio.gather(*(limits.consume(first.id, "tool_calls") for _ in range(12)),
                                   return_exceptions=True)
    assert sum(isinstance(r, int) for r in results) == 5
    assert sum(isinstance(r, limits.LimitExceeded) for r in results) == 7


# Uploads and download links through the real /files route ---------------------------------------


def _files_client():
    import httpx

    from nebula import server

    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _person_id(email: str) -> str:
    return str((await db.fetch_one("select id from people where email=%s", (email,)))["id"])


async def _charged(person_id: str) -> tuple[dict[str, int], int]:
    """Stored objects charged to a person, and the account total they should add up to."""
    rows = await db.fetch_all("select file_key, size_bytes from stored_objects where person_id=%s", (person_id,))
    acct = await db.fetch_one("select used_bytes from storage_accounts where person_id=%s", (person_id,))
    return {r["file_key"]: r["size_bytes"] for r in rows}, (acct["used_bytes"] if acct else 0)


async def test_upload_link_cannot_be_replayed(mcp):
    who = "replay@example.org"
    up = await call(mcp, who, "file", action="upload", filename="notes.txt", title="Replay notes")
    pid = await _person_id(who)
    async with _files_client() as c:
        r = await c.put(up["url"], content=b"The first body of the replay notes.")
        assert r.status_code == 200
        await drain()
        keys, used = await _charged(pid)
        assert list(keys) and not any(k.startswith("uploads/") for k in keys) and used == sum(keys.values())
        r = await c.put(up["url"], content=b"A different body sent later with the same link.")
        assert r.status_code == 409
    await drain()
    assert await _charged(pid) == (keys, used)


async def test_concurrent_puts_store_one_upload(mcp):
    import asyncio

    who = "double-put@example.org"
    up = await call(mcp, who, "file", action="upload", filename="double.txt", title="Double put")
    pid = await _person_id(who)
    async with _files_client() as c:
        rs = await asyncio.gather(c.put(up["url"], content=b"A" * 1000), c.put(up["url"], content=b"B" * 1000))
    assert sorted(r.status_code for r in rs) == [200, 409]
    await drain()
    st = await call(mcp, who, "file", action="status", upload_id=up["upload_id"])
    assert st["status"] == "done"
    keys, used = await _charged(pid)
    assert used == sum(keys.values()) == 1000


async def test_upload_that_fits_the_quota_once_is_indexed(mcp, monkeypatch):
    """The staged copy must not count twice while the canonical copy is written."""
    who = "tight-quota@example.org"
    up = await call(mcp, who, "file", action="upload", filename="tight.txt", title="Tight quota")
    pid = await _person_id(who)
    monkeypatch.setattr(settings(), "storage_bytes_per_person", 1500)
    async with _files_client() as c:
        r = await c.put(up["url"], content=b"tight quota words " * 55)  # 990 bytes
        assert r.status_code == 200
    await drain()
    st = await call(mcp, who, "file", action="status", upload_id=up["upload_id"])
    assert st["status"] == "done"
    assert (await _charged(pid))[1] == 990


async def test_failed_ingest_releases_storage(mcp, monkeypatch):
    from nebula import jobs
    from nebula.knowledge import index

    who = "bad-ingest@example.org"
    up = await call(mcp, who, "file", action="upload", filename="bad.txt", title="Bad ingest")
    pid = await _person_id(who)

    async def broken(*args, **kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(index, "replace_chunks", broken)
    async with _files_client() as c:
        assert (await c.put(up["url"], content=b"words that never get indexed " * 10)).status_code == 200
    key = f"upload:{up['upload_id']}"
    for _ in range(6):  # every retry, without waiting for the backoff
        await db.execute("update jobs set run_after=now() where status='queued' and dedupe_key=%s", (key,))
        await jobs.run_one()
    job = await db.fetch_one("select status from jobs where dedupe_key=%s", (key,))
    assert job["status"] == "failed"
    st = await call(mcp, who, "file", action="status", upload_id=up["upload_id"])
    assert st["status"] == "failed"
    assert await _charged(pid) == ({}, 0)


async def test_file_links_check_access_when_used(mcp):
    owner, reader = "links-owner@example.org", "links-reader@example.org"
    await call(mcp, reader, "people", groups=True)
    up = await call(mcp, owner, "file", action="upload", filename="shared.txt", title="Shared link file")
    async with _files_client() as c:
        assert (await c.put(up["url"], content=b"shared link file contents")).status_code == 200
        await drain()
        item = (await call(mcp, owner, "file", action="status", upload_id=up["upload_id"]))["item"]
        await call(mcp, owner, "share", id=item, add=[{"who": reader, "level": "view"}])
        dl = await call(mcp, reader, "file", action="download", id=item)
        assert (await c.get(dl["url"])).content == b"shared link file contents"
        # Removing the share stops the link it already handed out
        await call(mcp, owner, "share", id=item, remove=[reader])
        assert (await c.get(dl["url"])).status_code == 403
        # A deactivated account's download and upload links stop too
        dl = await call(mcp, owner, "file", action="download", id=item)
        up2 = await call(mcp, owner, "file", action="upload", filename="later.txt")
        await db.execute("update people set status='deactivated' where email=%s", (owner,))
        try:
            assert (await c.get(dl["url"])).status_code == 403
            assert (await c.put(up2["url"], content=b"too late")).status_code == 403
        finally:
            await db.execute("update people set status='active' where email=%s", (owner,))


async def test_daily_write_limit_blocks_writes(mcp, monkeypatch):
    who = "busy-writer@example.org"
    await call(mcp, who, "people", groups=True)
    monkeypatch.setattr(settings(), "writes_per_day", 2)
    for i in range(2):
        r = await call(mcp, who, "save", kind="record", type="fact", title=f"Busy fact {i}",
                       body=f"Fact number {i}.", confirm=True)
        assert r["saved"]
    r = await call(mcp, who, "save", kind="record", type="fact", title="One too many", body="x", confirm=True)
    assert "writes limit is reached" in r["error"]
    r = await call(mcp, who, "search", query="busy fact")
    assert "error" not in r


async def test_granola_notes_follow_who_was_in_the_meeting(mcp):
    """A Granola note is visible to the people in the meeting; the owning team only manages the collection
    unless it's published to them."""
    from nebula import access, org
    from nebula.connectors.base import FetchedItem
    from nebula.ingest.types import Block, Document
    from nebula.knowledge import items

    admin = await access.ensure_person(ADMIN)
    await access.ensure_person(MEMBER)
    await org.create_group(admin, "mtg", "Meetings team", lead_email="mtg-lead@example.org")
    lead = await access.ensure_person("mtg-lead@example.org")
    res = await org.create_collection(lead, slug="ap-meetings", name="AP meetings", kind="granola",
                                      source_config={"space_ids": ["spa_AP"]}, owner_group="mtg")
    assert "people who were in the meeting" in res["who_can_see"]
    await db.execute("update jobs set status='cancelled' where id=%s", (res["first_sync_job"],))
    col = await db.fetch_one("select * from collections where slug='ap-meetings'")
    note = FetchedItem(external_id="not_ap1", title="AP budget review", url="", mime_type="application/vnd.granola.note",
                       kind="meeting", document=Document(title="AP budget review", kind="meeting",
                                                         blocks=[Block("paragraph", "We cut the travel budget by a third.")]))
    async with db.tx() as conn:
        r = await items.upsert(conn, str(col["id"]), note)
        await items.apply_source_audience(conn, col, r["id"], {
            "emails": [MEMBER], "groups": [], "domains": [], "public": False, "unknown": False})
    await drain()
    assert await access.level_for(await access.get_person(MEMBER), "item", r["id"]) == "view"
    assert await access.level_for(lead, "item", r["id"]) == "none"
    out = await org.share(admin, "collection", str(col["id"]), [{"who": "mtg", "level": "view"}], None)
    assert out["changed"]
    assert await access.level_for(await access.get_person("mtg-lead@example.org"), "item", r["id"]) == "view"
