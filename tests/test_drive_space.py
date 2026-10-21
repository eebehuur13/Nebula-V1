"""The Drive source end to end: discovery, imports, refresh on request, lost and removed roots.

Drive itself is a fake that sits behind the connector registry; the real connector's own logic is
covered by the offline tests in test_connectors.py.
"""

from __future__ import annotations

from importlib import resources as pkg_resources

import pytest

from helpers import ADMIN, LEAD, MEMBER, call, drain
from nebula import connectors, db, jobs, sources
from nebula.connectors import gdrive
from nebula.connectors.base import Change, ChangeBatch, FetchedItem

BOT = "nebula-drive@example.iam.gserviceaccount.com"


def _audience(*emails: str) -> dict:
    return {"emails": list(emails), "groups": [], "domains": [], "public": False, "unknown": False}


class FakeDrive:
    """Files under roots, as the service account sees them."""

    files: dict[str, dict] = {}
    shared: list[dict] = []
    unreadable: set[str] = set()

    def __init__(self, config):
        self.roots = {r["id"]: r["kind"] for r in config.get("roots", [])}

    def _visible(self, fid: str) -> bool:
        f = self.files.get(fid)
        return bool(f) and f["root"] in self.roots and f["root"] not in self.unreadable

    def _change(self, fid: str) -> Change:
        f = self.files[fid]
        return Change(external_id=fid, title=f["title"], root=f["root"], rev=f["rev"], acl=f["acl"], path=[f["root"]])

    async def list_changes(self, cursor):
        lost = [r for r in self.roots if r in self.unreadable]
        return ChangeBatch([self._change(fid) for fid in self.files if self._visible(fid)], next_cursor="cursor",
                           lost_roots=lost)

    async def fetch(self, ch):
        f = self.files[ch.external_id]
        return FetchedItem(external_id=ch.external_id, title=f["title"], url="", mime_type="text/plain", kind="file",
                           data=f["text"].encode(), filename=f["title"] + ".txt",
                           extra={"audience": f["audience"], "rev": f["rev"], "acl": f["acl"]})

    async def file_audience(self, fid):
        return self.files[fid]["audience"]

    async def change_for(self, fid, cursor=None):
        return self._change(fid) if self._visible(fid) else None

    async def describe(self, fid):
        shared = next((s for s in self.shared if s["id"] == fid), {})
        caps = {"capabilities": {"canEdit": shared["editor"]}} if shared.get("editor") is not None else {}
        if fid in self.files:
            return {"id": fid, "name": self.files[fid]["title"], "mimeType": "text/plain", **caps}
        if shared:
            return {"id": fid, "name": fid, "mimeType": gdrive.FOLDER, **caps}
        return None

    async def covering_root(self, meta, cursor=None):
        f = self.files.get(meta["id"])
        return f["root"] if f and f["root"] in self.roots else None

    async def shared_with_nebula(self):
        return list(self.shared)

    async def close(self):
        return None


@pytest.fixture
async def drive(monkeypatch):
    FakeDrive.files = {
        "handbook01": {"root": "folder0001", "title": "Teacher handbook", "rev": "r1", "acl": "a",
                       "text": "Teachers meet every Saturday in Guntur.", "audience": _audience(MEMBER)},
        "salaries01": {"root": "folder0001", "title": "Salary sheet", "rev": "r1", "acl": "b",
                       "text": "Confidential salaries for the AP team.", "audience": _audience(LEAD)},
        "solofile01": {"root": "solofile01", "title": "Budget note", "rev": "r1", "acl": "c",
                       "text": "The Q3 budget is twelve lakh rupees.", "audience": _audience(MEMBER)},
    }
    FakeDrive.shared = [
        {"id": "folder0001", "name": "Team folder", "kind": "folder", "shared_by": LEAD},
        {"id": "solofile01", "name": "Budget note", "kind": "file", "shared_by": MEMBER},
    ]
    FakeDrive.unreadable = set()
    monkeypatch.setitem(connectors.CONNECTORS, "gdrive", FakeDrive)
    monkeypatch.setattr(gdrive, "service_account_email", lambda: BOT)
    yield FakeDrive
    # Nothing queued here should outlive the fake.
    await db.execute("update jobs set status='cancelled' where status='queued'")


async def _ids(who: str, query: str, mcp) -> set[str]:
    r = await call(mcp, who, "search", query=query)
    return {x["title"] for x in r.get("results", [])}


async def test_shared_folders_and_files_are_found_imported_and_kept_private(mcp, drive):
    found = await sources.discover()
    assert sorted(found["added"]) == ["Budget note", "Team folder"]
    space = await sources.drive_space()
    roots = {r["external_id"]: r for r in await db.fetch_all(
        "select r.*, p.email from source_roots r left join people p on p.id = r.managed_by where collection_id=%s",
        (space["id"],))}
    assert roots["folder0001"]["email"] == LEAD and roots["solofile01"]["email"] == MEMBER
    assert roots["folder0001"]["added_via"] == "discovery"
    await drain()

    # Each file is visible only to the people Drive shares it with.
    assert {"Teacher handbook", "Budget note"} <= await _ids(MEMBER, "Saturday teachers budget", mcp)
    assert "Salary sheet" not in await _ids(MEMBER, "salaries AP team", mcp)
    assert "Salary sheet" in await _ids(LEAD, "salaries AP team", mcp)
    assert "Teacher handbook" not in await _ids(LEAD, "Saturday teachers", mcp)
    # Stored files count against whoever shared the root.
    charged = await db.fetch_all(
        """select p.email, count(*) as n from stored_objects s join people p on p.id = s.person_id
           join items i on i.file_key = s.file_key where i.collection_id = %s group by p.email""", (space["id"],))
    assert {r["email"]: r["n"] for r in charged} == {LEAD: 2, MEMBER: 1}

    # Discovery again finds nothing new.
    assert (await sources.discover())["added"] == []

    r = await call(mcp, LEAD, "sources", action="list")
    assert [s["name"] for s in r["sources"]] == ["Team folder"] and BOT in r["how_to_add"]
    r = await call(mcp, ADMIN, "sources", action="list")
    assert {s["name"] for s in r["sources"]} >= {"Team folder", "Budget note"}


async def test_refresh_one_file_now_with_a_wait_estimate(mcp, drive):
    await sources.discover()
    await drain()
    handbook = await db.fetch_one("select id from items where external_id='handbook01' and status='active'")
    item = f"item:{handbook['id']}"

    drive.files["handbook01"].update(text="Teachers now meet every Sunday in Krishna.", rev="r2")
    r = await call(mcp, MEMBER, "sources", action="sync", id=item)
    assert r["job"] and r["job_status"] == "queued" and r["estimate"] and "job tool" in r["next"]
    j = await call(mcp, MEMBER, "job", id=r["job"])
    assert j["status"] == "queued" and "estimate" in j
    st = await call(mcp, MEMBER, "sources", action="status", id=item)
    assert st["refresh"]["job"] == r["job"]
    await drain()
    j = await call(mcp, MEMBER, "job", id=r["job"])
    assert j["status"] == "done" and j["result"]["ready"] and j["result"]["outcome"] == "changed"
    st = await call(mcp, MEMBER, "sources", action="status", id=item)
    assert st["search_ready"] and st["version"] == 2
    r = await call(mcp, MEMBER, "open", id=item)
    assert "Sunday" in r["_body"]

    # By Drive link too. Someone Drive doesn't share the file with can't refresh (or see) it.
    r = await call(mcp, MEMBER, "sources", action="sync", link="https://docs.google.com/document/d/handbook01/edit")
    assert r["id"] == item and r["job"]
    r = await call(mcp, LEAD, "sources", action="sync", id=item)
    assert "error" in r
    r = await call(mcp, LEAD, "sources", action="sync", link="https://docs.google.com/document/d/handbook01/edit")
    assert "Drive doesn't share it with you" in r["error"]
    # Something not shared with Nebula: say so, with the address to share it with.
    r = await call(mcp, MEMBER, "sources", action="sync", link="https://drive.google.com/file/d/notshared99/view")
    assert BOT in r["error"]
    r = await call(mcp, MEMBER, "sources", action="status", link="https://drive.google.com/file/d/notshared99/view")
    assert r["in_nebula"] is False
    # A file inside a folder Nebula already reads is refreshed, not added as a separate source.
    r = await call(mcp, MEMBER, "sources", action="connect", link="https://docs.google.com/document/d/handbook01/edit")
    assert r["already_shared_through"] == "Team folder" and r["job"]
    await drain()


async def test_lost_roots_leave_and_come_back_when_shared_again(mcp, drive):
    await sources.discover()
    await drain()
    space = await sources.drive_space()
    drive.unreadable = {"folder0001"}
    await jobs.enqueue("sync", {"collection_id": str(space["id"])}, dedupe_key="test-lost")
    await drain()
    root = await db.fetch_one("select status, status_note from source_roots where external_id='folder0001'")
    assert root["status"] == "lost" and "unshared" in root["status_note"]
    assert "Teacher handbook" not in await _ids(MEMBER, "Saturday teachers", mcp)

    drive.unreadable = set()
    found = await sources.discover()
    assert found["revived"] == ["Team folder"]
    await drain()
    assert "Teacher handbook" in await _ids(MEMBER, "Saturday teachers", mcp)


async def test_disconnected_roots_stay_out_until_connected_again(mcp, drive):
    await sources.discover()
    await drain()
    solo = await db.fetch_one("select id from source_roots where external_id='solofile01'")
    r = await call(mcp, LEAD, "sources", action="disconnect", source=str(solo["id"]))
    assert "error" in r  # only whoever shared it (or an admin)
    r = await call(mcp, MEMBER, "sources", action="disconnect", source=str(solo["id"]))
    assert r["status"] == "disconnected" and r["files_removed"] == 1
    await drain()
    assert "Budget note" not in await _ids(MEMBER, "budget lakh", mcp)
    assert (await sources.discover())["added"] == []  # still shared in Drive, but not picked up again

    # Asking for it explicitly brings it back.
    r = await call(mcp, MEMBER, "sources", action="connect", link="https://drive.google.com/file/d/solofile01/view")
    assert r["source"] == str(solo["id"]) and r["kind"] == "file" and r["job"]
    await drain()
    assert "Budget note" in await _ids(MEMBER, "budget lakh", mcp)


async def test_legacy_folder_collections_move_into_the_drive_source(mcp):
    """The 004 migration's conversion step, run against a folder connected the old way."""
    from nebula import access
    from nebula.knowledge import items

    owner = await access.ensure_person("legacy-owner@example.org")
    col = await db.fetch_one(
        """insert into collections (slug, name, kind, source_config, owner_person, created_by)
           values ('drive-legacy-folder', 'Legacy folder', 'gdrive', %s, %s, %s) returning *""",
        (db.jsonb({"folder_id": "legacyfolder01", "permissions": "mirror"}), owner.id, owner.id))
    story = FetchedItem(external_id="legacy-file", title="Legacy story", url="", mime_type="text/plain", kind="file",
                        data=b"An old folder's file.", filename="legacy.txt")
    async with db.tx() as conn:
        res = await items.upsert(conn, str(col["id"]), story)
    sql = (pkg_resources.files("nebula.migrations") / "004_drive_space.sql").read_text()
    conversion = sql[sql.index("create temp table legacy_drive"):]
    async with db.tx() as conn:
        await conn.execute(conversion)
    space = await sources.drive_space()
    moved = await db.fetch_one("select collection_id, source_root, status from items where id=%s", (res["id"],))
    assert str(moved["collection_id"]) == str(space["id"]) and moved["source_root"] == "legacyfolder01"
    assert moved["status"] == "active"
    chunks = await db.fetch_one("select count(*) as n from chunks where source_id=%s and collection_id=%s",
                                (res["id"], space["id"]))
    assert chunks["n"] >= 1
    root = await db.fetch_one("select * from source_roots where external_id='legacyfolder01'")
    assert str(root["managed_by"]) == owner.id and root["added_via"] == "migration" and root["kind"] == "folder"
    old = await db.fetch_one("select status from collections where id=%s", (col["id"],))
    assert old["status"] == "archived"


async def test_scheduler_queues_due_syncs_once(mcp, drive):
    from nebula import sync

    await sources.discover()
    space = await sources.drive_space()
    await db.execute("update jobs set status='cancelled' where status='queued'")
    await db.execute("update collections set last_synced_at = now() - interval '10 minutes' where id=%s", (space["id"],))
    assert await sync.schedule_due_syncs() >= 1
    queued = await db.fetch_all("select id from jobs where kind='sync' and status='queued' and payload->>'collection_id'=%s",
                                (str(space["id"]),))
    assert len(queued) == 1
    await sync.schedule_due_syncs()  # already queued: not again
    again = await db.fetch_all("select id from jobs where kind='sync' and status='queued' and payload->>'collection_id'=%s",
                               (str(space["id"]),))
    assert len(again) == 1
    await drain()
    checked = await sync.audience_check({}, {})
    assert "drive" not in checked  # files mirror their own sharing; nothing to compare at collection level


async def test_viewer_only_shares_are_flagged_and_rechecked_once_editor(mcp, drive):
    """As a Viewer, Nebula can't read who files are shared with (seen live on 20 Oct), so they stay hidden.
    Say so on the source, and re-read their sharing as soon as Nebula is made an Editor."""
    drive.shared = [{"id": "viewfold01", "name": "Viewer folder", "kind": "folder", "shared_by": LEAD, "editor": False}]
    await sources.discover()
    root = await db.fetch_one("select id, status_note from source_roots where external_id='viewfold01'")
    assert root["status_note"] == sources.VIEWER_NOTE
    r = await call(mcp, LEAD, "sources", action="list")
    assert any(x["name"] == "Viewer folder" and "Editor" in x["note"] for x in r["sources"])
    assert (await sources.discover())["now_editor"] == []

    drive.shared[0]["editor"] = True
    found = await sources.discover()
    assert found["now_editor"] == ["viewfold01"]
    root = await db.fetch_one("select status_note from source_roots where external_id='viewfold01'")
    assert root["status_note"] == ""
    job = await db.fetch_one("select payload from jobs where dedupe_key like 'sync-upgraded:%%' order by created_at desc")
    assert job["payload"]["rewalk"] == ["viewfold01"]

    # Connecting something shared only as Viewer says so straight away.
    drive.shared.append({"id": "viewfold02", "name": "Another", "kind": "folder", "shared_by": LEAD, "editor": False})
    r = await call(mcp, LEAD, "sources", action="connect", link="https://drive.google.com/drive/folders/viewfold02")
    assert r["warning"] == sources.VIEWER_NOTE and r["job"]
    r = await call(mcp, LEAD, "sources", action="connect", link="https://drive.google.com/file/d/notshared77/view")
    assert "as an Editor" in r["error"] and "only ever reads" in r["error"]
