"""Offline tests for the Drive, Notion and Granola connectors (every HTTP call is mocked)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from nebula.config import settings
from nebula.connectors import (
    GoogleDriveConnector,
    GranolaConnector,
    NotionConnector,
    get_connector,
)
from nebula.connectors import _http, gdrive
from nebula.connectors._http import SourceError
from nebula.connectors.base import Change

FOLDER = gdrive.FOLDER
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"


# ---------------------------------------------------------------------------
# Shared fixtures


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make backoff and rate limiting instant; record the delays asked for."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(_http, "sleep", fake_sleep)
    return delays


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEBULA_NOTION_TOKEN", "secret-notion")
    monkeypatch.setenv("NEBULA_GRANOLA_API_KEY", "grn_secret")
    settings.cache_clear()
    yield
    settings.cache_clear()


class FakeCredentials:
    """Stands in for google-auth service-account credentials."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.valid = False
        self.refreshes = 0
        self.service_account_email = "nebula@example-project.iam.gserviceaccount.com"

    def refresh(self, request: Any) -> None:
        self.refreshes += 1
        self.token, self.valid = "test-token", True


@pytest.fixture
def fake_creds(monkeypatch: pytest.MonkeyPatch) -> FakeCredentials:
    creds = FakeCredentials()
    monkeypatch.setattr(gdrive, "load_credentials", lambda: creds)
    return creds


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def ok(data: Any, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)


# ---------------------------------------------------------------------------
# Google Drive


class FakeDrive:
    """Minimal Drive v3: files.list keyed by `q` (pages of results), files.get, export, changes."""

    def __init__(self, router: respx.MockRouter):
        self.lists: dict[str, list[list[dict[str, Any]]]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.content: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.auth_headers: set[str] = set()
        self.permissions: dict[str, list[dict[str, Any]] | None] = {}  # None = caller can't list them
        host = "www.googleapis.com"
        router.get(host=host, path="/drive/v3/files").mock(side_effect=self.list)
        router.get(host=host, path__regex=r"^/drive/v3/files/(?P<file_id>[^/]+)$").mock(side_effect=self.get)
        router.get(host=host, path__regex=r"^/drive/v3/files/(?P<file_id>[^/]+)/export$").mock(side_effect=self.export)
        router.get(host=host, path__regex=r"^/drive/v3/files/(?P<file_id>[^/]+)/permissions$").mock(
            side_effect=self.perms)

    def _seen(self, request: httpx.Request) -> None:
        self.auth_headers.add(request.headers.get("authorization", ""))

    def list(self, request: httpx.Request) -> httpx.Response:
        self._seen(request)
        params = request.url.params
        assert params["supportsAllDrives"] == "true" and params["includeItemsFromAllDrives"] == "true"
        pages = self.lists.get(params["q"], [[]])
        index = int(params.get("pageToken") or 0)
        body: dict[str, Any] = {"files": pages[index]}
        if index + 1 < len(pages):
            body["nextPageToken"] = str(index + 1)
        return ok(body)

    def get(self, request: httpx.Request, file_id: str) -> httpx.Response:
        self._seen(request)
        if file_id not in self.files:
            return ok({"error": {"code": 404, "errors": [{"reason": "notFound"}]}}, 404)
        if request.url.params.get("alt") == "media":
            self.downloads.append(file_id)
            return httpx.Response(200, content=self.content[file_id])
        return ok(self.files[file_id])

    def perms(self, request: httpx.Request, file_id: str) -> httpx.Response:
        self._seen(request)
        listed = self.permissions.get(file_id, [])
        if listed is None:
            return ok({"error": {"code": 403, "errors": [{"reason": "insufficientFilePermissions"}]}}, 403)
        return ok({"permissions": listed})

    def export(self, request: httpx.Request, file_id: str) -> httpx.Response:
        self._seen(request)
        self.downloads.append(file_id)
        assert request.url.params["mimeType"] == "text/markdown"
        return httpx.Response(200, content=self.content[file_id])


def drive_file(fid: str, name: str, mime: str, parent: str, **extra: Any) -> dict[str, Any]:
    return {"id": fid, "name": name, "mimeType": mime, "parents": [parent],
            "modifiedTime": "2026-10-01T10:00:00Z", "createdTime": "2026-09-01T10:00:00Z",
            "webViewLink": f"https://drive.google.com/{fid}", **extra}


FOLDERS_Q = f"mimeType = '{FOLDER}' and trashed = false"
FILES_Q = f"mimeType != '{FOLDER}' and trashed = false"


async def test_drive_full_listing_of_shared_drive(router, fake_creds):
    drive = FakeDrive(router)
    router.get("https://www.googleapis.com/drive/v3/drives/drv1").mock(return_value=ok({"id": "drv1", "name": "Programme"}))
    router.get("https://www.googleapis.com/drive/v3/changes/startPageToken").mock(return_value=ok({"startPageToken": "100"}))
    drive.lists[FOLDERS_Q] = [
        [drive_file("fA", "AP", FOLDER, "drv1"), drive_file("fB", "Reports", FOLDER, "fA")],
        [drive_file("fL", "HR", FOLDER, "drv1", inheritedPermissionsDisabled=True)],
    ]
    drive.lists[FILES_Q] = [
        [drive_file("d1", "Plan", DOC, "fB"), drive_file("s1", "Budget", SHEET, "fA"),
         drive_file("p1", "deck.pdf", "application/pdf", "drv1", size="1000")],
        [drive_file("x1", "salaries.docx", gdrive.DOCX, "fL", size="2000"),
         drive_file("v1", "clip.mp4", "video/mp4", "fA", size="10"),
         drive_file("big", "huge.pdf", "application/pdf", "fA", size=str(60 * 1024 * 1024)),
         drive_file("sc", "link", "application/vnd.google-apps.shortcut", "fA")],
    ]
    connector = GoogleDriveConnector({"drive_id": "drv1"})

    first = await connector.list_changes(None)
    assert first.has_more
    by_id = {c.external_id: c for c in first.changes}
    assert set(by_id) == {"d1", "s1", "p1"}
    assert by_id["d1"].path == ["Programme", "AP", "Reports"]
    assert by_id["s1"].path == ["Programme", "AP"]
    assert by_id["p1"].path == ["Programme"]
    assert not any(c.narrower_than_root for c in first.changes)

    second = await connector.list_changes(first.next_cursor)
    assert not second.has_more
    assert [c.external_id for c in second.changes] == ["x1"]  # mp4, >50 MB pdf and shortcut skipped
    assert second.changes[0].narrower_than_root
    assert second.changes[0].path == ["Programme", "HR"]

    assert by_id["d1"].root == "drv1"
    state = json.loads(second.next_cursor)
    assert state["v"] == 2 and not state["full"] and state["token"] == "100" and state["drives"] == []
    assert state["roots"] == {"drv1": "drive"}
    assert set(state["folders"]) == {"drv1", "fA", "fB", "fL"}
    assert fake_creds.refreshes == 1 and drive.auth_headers == {"Bearer test-token"}
    await connector.close()


async def test_drive_incremental_changes_folder_root(router, fake_creds):
    drive = FakeDrive(router)
    cursor = json.dumps({
        "v": 2, "roots": {"root1": "folder"}, "token": "100", "queue": [], "pending": [], "drives": [], "full": False,
        "folders": {"root1": ["Root", "", 0, "", ""], "fA": ["AP", "root1", 0, "", ""],
                    "fB": ["Reports", "fA", 0, "", ""]},
    })
    changes = [
        {"changeType": "file", "fileId": "d1", "removed": False, "file": drive_file("d1", "Plan", DOC, "fA")},
        {"changeType": "file", "fileId": "gone1", "removed": True},
        # moved out of the root: its new parent is a folder outside the collection
        {"changeType": "file", "fileId": "m1", "removed": False, "file": drive_file("m1", "Moved", DOC, "elsewhere")},
        # a whole folder moved out: everything below it goes
        {"changeType": "file", "fileId": "fB", "removed": False, "file": drive_file("fB", "Reports", FOLDER, "elsewhere")},
        {"changeType": "file", "fileId": "t1", "removed": False, "file": drive_file("t1", "Old", DOC, "fA", trashed=True)},
        # a new folder inside the root: its contents are listed
        {"changeType": "file", "fileId": "fN", "removed": False, "file": drive_file("fN", "New", FOLDER, "fA")},
    ]
    router.get("https://www.googleapis.com/drive/v3/changes").mock(
        return_value=ok({"changes": changes, "newStartPageToken": "200"}))
    drive.files["elsewhere"] = {"id": "elsewhere", "name": "Someone's folder", "mimeType": FOLDER}
    drive.lists["'fB' in parents"] = [[drive_file("d2", "Q2", DOC, "fB")]]
    drive.lists["'fN' in parents and trashed = false"] = [[drive_file("n1", "note.pdf", "application/pdf", "fN", size="5")]]

    connector = GoogleDriveConnector({"folder_id": "root1"})
    batch = await connector.list_changes(cursor)
    by_id = {c.external_id: c for c in batch.changes}

    assert not by_id["d1"].deleted and by_id["d1"].path == ["Root", "AP"]
    assert by_id["gone1"].deleted
    assert by_id["m1"].deleted
    assert by_id["d2"].deleted
    assert by_id["t1"].deleted
    assert not by_id["n1"].deleted and by_id["n1"].path == ["Root", "AP", "New"]
    assert not batch.has_more
    state = json.loads(batch.next_cursor)
    assert state["token"] == "200"
    assert "fB" not in state["folders"] and state["folders"]["fN"] == ["New", "fA", 0, "", ""]
    await connector.close()


def drive_cursor(roots: dict[str, str], folders: dict[str, list[Any]], token: str = "100") -> str:
    return json.dumps({"v": 2, "roots": roots, "token": token, "queue": [], "pending": [], "drives": [],
                       "full": False, "folders": folders})


async def test_drive_many_roots_folder_file_and_new_root(router, fake_creds):
    """Folders and single files shared with Nebula are roots of one source; a root added later is walked
    on the next sync while the change feed covers the rest."""
    drive = FakeDrive(router)
    router.get("https://www.googleapis.com/drive/v3/changes/startPageToken").mock(return_value=ok({"startPageToken": "7"}))
    router.get("https://www.googleapis.com/drive/v3/changes").mock(
        return_value=ok({"changes": [], "newStartPageToken": "8"}))
    drive.files["rootA"] = drive_file("rootA", "Team A", FOLDER, "someone", permissionIds=["p1"])
    drive.files["solo"] = drive_file("solo", "Budget note", DOC, "private", permissionIds=["p9", "p2"])
    drive.lists["'rootA' in parents and trashed = false"] = [[
        drive_file("a1", "Plan", DOC, "rootA", permissionIds=["p1"], md5Checksum="x"),
        drive_file("subB", "Sub", FOLDER, "rootA"),
    ]]
    drive.lists["'subB' in parents and trashed = false"] = [[drive_file("b1", "Notes", DOC, "subB")]]

    connector = GoogleDriveConnector({"roots": [{"id": "rootA", "kind": "folder"}, {"id": "solo", "kind": "file"}]})
    batch = await connector.list_changes(None)
    by_id = {c.external_id: c for c in batch.changes}
    assert set(by_id) == {"a1", "b1", "solo"} and not batch.has_more and not batch.lost_roots
    assert by_id["solo"].root == "solo" and by_id["solo"].path == [] and by_id["solo"].acl == "p2,p9"
    assert by_id["b1"].root == "rootA" and by_id["b1"].path == ["Team A", "Sub"]
    assert by_id["a1"].rev == "2026-10-01T10:00:00Z|x" and by_id["a1"].acl == "p1"
    assert by_id["b1"].acl is None  # Drive didn't say: the worker falls back to reading sharing on full listings
    await connector.close()

    # A third root shared later: walked first, then the change feed in the same sync.
    drive.files["rootC"] = drive_file("rootC", "Team C", FOLDER, "elsewhere")
    drive.lists["'rootC' in parents and trashed = false"] = [[drive_file("c1", "Minutes", DOC, "rootC")]]
    connector = GoogleDriveConnector({"roots": [{"id": "rootA", "kind": "folder"}, {"id": "solo", "kind": "file"},
                                                {"id": "rootC", "kind": "folder"}]})
    walked = await connector.list_changes(batch.next_cursor)
    assert [c.external_id for c in walked.changes] == ["c1"] and walked.has_more
    feed = await connector.list_changes(walked.next_cursor)
    assert feed.changes == [] and not feed.has_more and json.loads(feed.next_cursor)["token"] == "8"
    await connector.close()

    # A root that's gone from the config is forgotten; one Nebula can't open is reported as lost.
    connector = GoogleDriveConnector({"roots": [{"id": "rootA", "kind": "folder"}, {"id": "ghost", "kind": "folder"}]})
    later = await connector.list_changes(feed.next_cursor)
    assert later.lost_roots == ["ghost"]
    assert set(json.loads(later.next_cursor)["roots"]) == {"rootA"}
    await connector.close()


async def test_drive_root_unshared_and_folder_resharing(router, fake_creds):
    drive = FakeDrive(router)
    cursor = drive_cursor({"rootA": "folder", "rootB": "folder"}, {
        "rootA": ["A", "", 0, "", "p1"], "fX": ["X", "rootA", 0, "", "p1"],
        "rootB": ["B", "", 0, "", "p5"],
    })
    changes = [
        # Someone shared folder X with another person: everything below it gets its sharing re-read.
        {"changeType": "file", "fileId": "fX", "removed": False,
         "file": drive_file("fX", "X", FOLDER, "rootA", permissionIds=["p1", "p7"])},
        # Root B was unshared from Nebula.
        {"changeType": "file", "fileId": "rootB", "removed": True},
    ]
    router.get("https://www.googleapis.com/drive/v3/changes").mock(
        return_value=ok({"changes": changes, "newStartPageToken": "101"}))
    drive.lists["'fX' in parents and trashed = false"] = [[drive_file("x1", "Doc", DOC, "fX", permissionIds=["p1"])]]
    drive.lists["'rootB' in parents"] = [[drive_file("b1", "Old", DOC, "rootB")]]
    connector = GoogleDriveConnector({"roots": [{"id": "rootA", "kind": "folder"}, {"id": "rootB", "kind": "folder"}]})
    batch = await connector.list_changes(cursor)
    by_id = {c.external_id: c for c in batch.changes}
    assert by_id["x1"].recheck_acl and not by_id["x1"].deleted
    assert by_id["b1"].deleted and batch.lost_roots == ["rootB"]
    await connector.close()


async def test_drive_one_file_and_discovery(router, fake_creds):
    drive = FakeDrive(router)
    cursor = drive_cursor({"rootA": "folder"}, {"rootA": ["A", "", 0, "", ""], "fX": ["X", "rootA", 0, "", ""]})
    drive.files["x1"] = drive_file("x1", "Doc", DOC, "fX", permissionIds=["p1"])
    drive.files["lone"] = drive_file("lone", "Lone", DOC, "nowhere")
    connector = GoogleDriveConnector({"roots": [{"id": "rootA", "kind": "folder"}]})
    change = await connector.change_for("x1", cursor)
    assert change is not None and change.root == "rootA" and change.path == ["A", "X"]
    assert await connector.change_for("lone", cursor) is None  # readable, but not under anything shared
    assert await connector.change_for("missing", cursor) is None
    assert await connector.covering_root({"id": "fX", "parents": ["rootA"]}, cursor) == "rootA"

    drive.lists["sharedWithMe and trashed = false"] = [[
        drive_file("fold", "Shared folder", FOLDER, "x", sharingUser={"emailAddress": "Priya@example.org"},
                   capabilities={"canEdit": True}),
        drive_file("doc", "Shared doc", DOC, "x", sharingUser={"emailAddress": "asha@example.org"},
                   capabilities={"canEdit": False}),
        drive_file("pic", "photo.jpg", "image/jpeg", "x", size="10"),
    ]]
    router.get("https://www.googleapis.com/drive/v3/drives").mock(
        return_value=ok({"drives": [{"id": "drv9", "name": "Programme"}]}))
    found = await connector.shared_with_nebula()
    assert found == [
        {"id": "fold", "name": "Shared folder", "kind": "folder", "shared_by": "priya@example.org", "editor": True},
        {"id": "doc", "name": "Shared doc", "kind": "file", "shared_by": "asha@example.org", "editor": False},
        {"id": "drv9", "name": "Programme", "kind": "drive", "shared_by": "", "editor": None},
    ]
    await connector.close()


def test_drive_links():
    fid = "1ctsTrdHykTIv-phcl6rwvHPYQbu1_kGl"
    for link in (f"https://drive.google.com/drive/folders/{fid}", f"https://drive.google.com/drive/u/0/folders/{fid}?usp=x",
                 f"https://docs.google.com/document/d/{fid}/edit", f"https://drive.google.com/file/d/{fid}/view",
                 f"https://drive.google.com/open?id={fid}", fid):
        assert gdrive.drive_id_from(link) == fid
    with pytest.raises(ValueError):
        gdrive.drive_id_from("https://example.com/nope")


async def test_drive_fetch_exports_doc_as_markdown(router, fake_creds):
    drive = FakeDrive(router)
    drive.files["d1"] = drive_file("d1", "Plan", DOC, "fA", owners=[{"emailAddress": "asha@example.org"}], version="7")
    drive.content["d1"] = b"# Plan\n\nHello"
    connector = GoogleDriveConnector({"drive_id": "drv1"})

    item = await connector.fetch(Change(external_id="d1", path=["Programme", "AP"]))
    assert item is not None
    assert item.filename == "Plan.md" and item.mime_type == "text/markdown" and item.kind == "doc"
    assert item.data == b"# Plan\n\nHello"
    assert item.url == "https://drive.google.com/d1" and item.author == "asha@example.org"
    assert item.path == ["Programme", "AP"] and item.extra["version"] == "7"
    assert item.extra["audience"] == {"emails": [], "groups": [], "domains": [], "public": False, "unknown": False}
    await connector.close()


async def test_drive_fetch_reports_file_sharing(router, fake_creds):
    drive = FakeDrive(router)
    drive.files["d1"] = drive_file("d1", "Story", "text/plain", "fA", size="5")
    drive.content["d1"] = b"hello"
    drive.permissions["d1"] = [
        {"type": "user", "role": "writer", "emailAddress": "Priya@Example.org"},
        {"type": "user", "role": "reader", "emailAddress": "friend@example.com"},
        {"type": "group", "role": "reader", "emailAddress": "ap-team@example.org"},
        {"type": "domain", "role": "reader", "domain": "example.org"},
        {"type": "anyone", "role": "reader"},
        {"type": "user", "role": "reader", "emailAddress": "gone@x.org", "deleted": True},
    ]
    drive.files["d2"] = drive_file("d2", "Locked", "text/plain", "fA", size="5")
    drive.content["d2"] = b"hello"
    drive.permissions["d2"] = None
    connector = GoogleDriveConnector({"drive_id": "drv1"})

    item = await connector.fetch(Change(external_id="d1"))
    assert item.extra["audience"] == {
        "emails": ["friend@example.com", "priya@example.org"], "groups": ["ap-team@example.org"],
        "domains": ["example.org"], "public": True, "unknown": False}
    locked = await connector.fetch(Change(external_id="d2"))
    assert locked.extra["audience"]["unknown"] is True
    await connector.close()


async def test_drive_fetch_skips_unsupported_types(router, fake_creds):
    drive = FakeDrive(router)
    drive.files["v1"] = drive_file("v1", "clip.mp4", "video/mp4", "fA", size="10")
    drive.files["f1"] = drive_file("f1", "Survey", "application/vnd.google-apps.form", "fA")
    connector = GoogleDriveConnector({"drive_id": "drv1"})

    assert await connector.fetch(Change(external_id="v1")) is None
    assert await connector.fetch(Change(external_id="f1")) is None
    assert await connector.fetch(Change(external_id="missing")) is None
    assert drive.downloads == []
    await connector.close()


def test_drive_classify():
    assert gdrive.classify({"name": "a", "mimeType": SHEET}) == gdrive.Plan("sheet", ".xlsx", gdrive.XLSX)
    assert gdrive.classify({"name": "notes.TXT", "mimeType": "text/plain", "size": "3"}).kind == "file"
    assert gdrive.classify({"name": "report", "mimeType": "application/pdf", "size": "3"}).ext == ".pdf"
    assert gdrive.classify({"name": "data.csv", "mimeType": "text/csv"}).kind == "sheet"
    assert gdrive.classify({"name": "x.zip", "mimeType": "application/zip"}) is None
    assert gdrive.classify({"name": "f", "mimeType": FOLDER}) is None


async def test_drive_backs_off_on_rate_limit(router, fake_creds, sleeps):
    rate_limited = ok({"error": {"code": 403, "errors": [{"reason": "userRateLimitExceeded"}]}}, 403)
    router.get("https://www.googleapis.com/drive/v3/changes/startPageToken").mock(
        side_effect=[rate_limited, ok({}, 429), ok({"startPageToken": "9"})])
    connector = GoogleDriveConnector({"drive_id": "drv1"})
    assert await connector._start_token("drv1") == "9"
    assert len(sleeps) == 2 and all(d > 0 for d in sleeps)
    await connector.close()


async def test_drive_source_audience(router, fake_creds):
    permissions = [
        {"type": "user", "role": "organizer", "emailAddress": "Lead@example.org"},
        {"type": "user", "role": "reader", "emailAddress": "nebula@example-project.iam.gserviceaccount.com"},
        {"type": "group", "role": "writer", "emailAddress": "programme@example.org"},
        {"type": "domain", "role": "reader", "domain": "example.org"},
    ]
    route = router.get("https://www.googleapis.com/drive/v3/files/drv1/permissions").mock(
        return_value=ok({"permissions": permissions}))
    connector = GoogleDriveConnector({"drive_id": "drv1"})

    audience = await connector.source_audience()
    assert audience.emails == {"lead@example.org"}
    assert audience.groups == {"programme@example.org"}
    assert audience.domain_wide and not audience.public and "example.org" in audience.note
    assert route.calls[0].request.url.params["useDomainAdminAccess"] == "false"
    await connector.close()


# ---------------------------------------------------------------------------
# Notion

NOTION = "https://api.notion.com/v1"
R, X, P1, P2, P3, P4, DS1, ROW1, B1 = (
    "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
    "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444",
    "55555555-5555-5555-5555-555555555555", "66666666-6666-6666-6666-666666666666",
    "77777777-7777-7777-7777-777777777777", "88888888-8888-8888-8888-888888888888",
    "99999999-9999-9999-9999-999999999999",
)


def rt(text: str, href: str | None = None) -> list[dict[str, Any]]:
    return [{"type": "text", "plain_text": text, "href": href}]


def notion_page(pid: str, title: str, parent: dict[str, Any], edited: str = "2026-10-10T10:00:00.000Z", **extra: Any) -> dict[str, Any]:
    return {"object": "page", "id": pid, "parent": parent, "last_edited_time": edited,
            "created_time": "2026-01-01T00:00:00.000Z", "in_trash": False, "is_archived": False,
            "url": f"https://www.notion.so/{pid.replace('-', '')}", "created_by": {"object": "user", "id": "u1"},
            "properties": {"Name": {"id": "title", "type": "title", "title": rt(title)}}, **extra}


def page_parent(pid: str) -> dict[str, Any]:
    return {"type": "page_id", "page_id": pid}


def search_router(router: respx.MockRouter, normal: list[dict[str, Any]], trashed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        assert request.headers["Notion-Version"] == "2026-03-11"
        assert body["sort"] == {"timestamp": "last_edited_time", "direction": "descending"}
        results = trashed if (body.get("filter") or {}).get("in_trash") else normal
        if body.get("start_cursor") == "c2":
            return ok({"object": "list", "results": results[2:], "has_more": False, "next_cursor": None})
        return ok({"object": "list", "results": results[:2], "has_more": len(results) > 2,
                   "next_cursor": "c2" if len(results) > 2 else None})

    router.post(f"{NOTION}/search").mock(side_effect=handler)
    return bodies


async def test_notion_search_keeps_descendants_of_root(router):
    root = notion_page(R, "Handbook", {"type": "workspace", "workspace": True})
    other = notion_page(X, "Elsewhere", {"type": "workspace", "workspace": True})
    p1 = notion_page(P1, "Intro", page_parent(R), "2026-10-12T10:00:00.000Z")
    p2 = notion_page(P2, "Not ours", page_parent(X), "2026-10-11T10:00:00.000Z")
    row = notion_page(ROW1, "Task A", {"type": "data_source_id", "data_source_id": DS1, "database_id": "db1"},
                      "2026-10-11T09:00:00.000Z")
    ds = {"object": "data_source", "id": DS1, "title": rt("Tracker"), "parent": {"type": "database_id", "database_id": "db1"},
          "database_parent": page_parent(R), "last_edited_time": "2026-10-10T09:00:00.000Z", "in_trash": False,
          "url": "https://www.notion.so/tracker", "properties": {}}
    p3 = notion_page(P3, "Nested", {"type": "block_id", "block_id": B1}, "2026-10-10T08:00:00.000Z")
    p4 = notion_page(P4, "Archived", page_parent(R), "2026-10-10T07:00:00.000Z", is_archived=True)
    bodies = search_router(router, [p1, p2, row, ds, p3, p4], [])
    router.get(f"{NOTION}/pages/{R}").mock(return_value=ok(root))
    router.get(f"{NOTION}/pages/{X}").mock(return_value=ok(other))
    router.get(f"{NOTION}/pages/{P1}").mock(return_value=ok(p1))
    router.get(f"{NOTION}/data_sources/{DS1}").mock(return_value=ok(ds))
    router.get(f"{NOTION}/blocks/{B1}").mock(return_value=ok({"object": "block", "id": B1, "type": "column", "parent": page_parent(P1)}))

    connector = NotionConnector({"root_page_id": R.replace("-", "")})
    batch = await connector.list_changes(None)
    by_id = {c.external_id: c for c in batch.changes}

    assert set(by_id) == {P1, ROW1, DS1, P3, P4}
    assert by_id[P1].path == ["Handbook"]
    assert by_id[ROW1].path == ["Handbook", "Tracker"]
    assert by_id[DS1].mime_type == "application/vnd.notion.data-source"
    assert by_id[P3].path == ["Handbook", "Intro"]
    assert by_id[P4].deleted
    assert json.loads(batch.next_cursor)["hwm"].startswith("2026-10-12T10:00")
    assert len(bodies) == 2 and not any("filter" in b for b in bodies)  # full listing: no trash pass
    await connector.close()


async def test_notion_leaves_out_excluded_page_trees(router):
    root = notion_page(R, "Handbook", {"type": "workspace", "workspace": True})
    p1 = notion_page(P1, "HR (restricted)", page_parent(R), "2026-10-12T10:00:00.000Z")
    p2 = notion_page(P2, "Onboarding", page_parent(R), "2026-10-11T10:00:00.000Z")
    p3 = notion_page(P3, "Salaries", page_parent(P1), "2026-10-10T10:00:00.000Z")
    search_router(router, [p1, p2, p3], [])
    router.get(f"{NOTION}/pages/{R}").mock(return_value=ok(root))
    router.get(f"{NOTION}/pages/{P1}").mock(return_value=ok(p1))
    connector = NotionConnector({"root_page_id": R, "exclude_page_ids": [P1.replace("-", "")]})
    batch = await connector.list_changes(None)
    assert [c.external_id for c in batch.changes] == [P2]
    await connector.close()


async def test_notion_incremental_stops_at_high_water_and_reports_trash(router):
    recent = notion_page(P1, "Intro", page_parent(R), "2026-10-12T10:00:00.000Z")
    moved_out = notion_page(P3, "Moved", page_parent(X), "2026-10-11T10:00:00.000Z")
    old = notion_page(P2, "Old", page_parent(R), "2026-10-01T10:00:00.000Z")
    trashed = notion_page(P4, "Binned", page_parent(R), "2026-10-12T11:00:00.000Z", in_trash=True)
    bodies = search_router(router, [recent, moved_out, old], [trashed])
    workspace = {"type": "workspace", "workspace": True}
    router.get(f"{NOTION}/pages/{R}").mock(return_value=ok(notion_page(R, "Handbook", workspace)))
    router.get(f"{NOTION}/pages/{X}").mock(return_value=ok(notion_page(X, "Private", workspace)))

    connector = NotionConnector({"root_page_id": R})
    batch = await connector.list_changes(json.dumps({"hwm": "2026-10-10T10:00:00+00:00"}))
    by_id = {c.external_id: c for c in batch.changes}
    assert set(by_id) == {P1, P3, P4}  # P2 is older than the high-water mark minus the window
    assert by_id[P4].deleted and by_id[P3].deleted and not by_id[P1].deleted
    assert bodies[-1]["filter"] == {"in_trash": True}
    assert json.loads(batch.next_cursor)["hwm"].startswith("2026-10-12T11:00")
    await connector.close()


def block(bid: str, kind: str, has_children: bool = False, **data: Any) -> dict[str, Any]:
    return {"object": "block", "id": bid, "type": kind, "has_children": has_children, kind: data}


async def test_notion_fetch_builds_document_from_block_tree(router):
    top = [
        block("h1", "heading_1", rich_text=rt("Overview")),
        block("h2", "heading_2", rich_text=rt("Goals")),
        block("h3", "heading_3", rich_text=rt("Detail")),
        block("p", "paragraph", rich_text=rt("See ") + rt("the site", "https://rl.org")),
        block("b1", "bulleted_list_item", True, rich_text=rt("Apple")),
        block("n1", "numbered_list_item", rich_text=rt("One")),
        block("n2", "numbered_list_item", rich_text=rt("Two")),
        block("td", "to_do", rich_text=rt("Done"), checked=True),
        block("tg", "toggle", True, rich_text=rt("More")),
        block("dv", "divider"),
        block("tb", "table", True, table_width=2, has_column_header=True),
        block("cp", "child_page", True, title="Sub"),
        block("im", "image", type="file", caption=rt("A chart"), file={"url": "https://signed"}),
        block("cd", "code", rich_text=rt("print(1)"), language="python"),
    ]
    children = {
        P1: top,
        "b1": [block("b1a", "bulleted_list_item", rich_text=rt("Green"))],
        "tg": [block("tgp", "paragraph", rich_text=rt("Hidden text"))],
        "tb": [block("r1", "table_row", cells=[rt("State"), rt("Centres")]),
               block("r2", "table_row", cells=[rt("AP"), rt("120")])],
    }
    for parent_id, items in children.items():
        router.get(f"{NOTION}/blocks/{parent_id}/children").mock(
            return_value=ok({"object": "list", "results": items, "has_more": False, "next_cursor": None}))
    router.get(f"{NOTION}/pages/{P1}").mock(return_value=ok(notion_page(P1, "Intro", page_parent(R))))
    router.get(f"{NOTION}/users/u1").mock(return_value=ok({"object": "user", "id": "u1", "name": "Asha"}))

    connector = NotionConnector({"root_page_id": R})
    item = await connector.fetch(Change(external_id=P1, mime_type="application/vnd.notion.page", path=["Handbook"]))
    assert item is not None and item.document is not None
    blocks = item.document.blocks
    assert item.kind == "notion_page" and item.author == "Asha" and item.path == ["Handbook"]
    assert [(b.text, b.level) for b in blocks if b.kind == "heading"] == [("Overview", 1), ("Goals", 2), ("Detail", 3)]
    assert blocks[3].text == "See [the site](https://rl.org)"
    lists = [b for b in blocks if b.kind == "list"]
    assert len(lists) == 1
    assert lists[0].text == "- Apple\n  - Green\n1. One\n2. Two\n- [x] Done"
    assert lists[0].locator == {"block_id": "b1"}
    texts = [b.text for b in blocks]
    assert texts.index("More") + 1 == texts.index("Hidden text")  # toggle text, then its children
    table = next(b for b in blocks if b.kind == "table")
    assert table.table == [["State", "Centres"], ["AP", "120"]]
    assert "Sub-page: Sub" in texts
    assert any(b.kind == "image" and b.text == "A chart" for b in blocks)
    assert any(b.kind == "code" and b.text == "print(1)" for b in blocks)
    assert all(b.locator.get("block_id") for b in blocks)
    await connector.close()


async def test_notion_database_row_gets_properties_table(router):
    row = notion_page(ROW1, "Task A", {"type": "data_source_id", "data_source_id": DS1, "database_id": "db1"})
    row["properties"] |= {
        "Status": {"type": "status", "status": {"name": "Doing"}},
        "Tags": {"type": "multi_select", "multi_select": [{"name": "AP"}, {"name": "Pilot"}]},
        "Empty": {"type": "rich_text", "rich_text": []},
    }
    router.get(f"{NOTION}/pages/{ROW1}").mock(return_value=ok(row))
    router.get(f"{NOTION}/blocks/{ROW1}/children").mock(return_value=ok({"results": [], "has_more": False}))
    router.get(f"{NOTION}/users/u1").mock(return_value=ok({}, 403))

    connector = NotionConnector({"data_source_ids": [DS1]})
    item = await connector.fetch(Change(external_id=ROW1, mime_type="application/vnd.notion.page"))
    assert item is not None and item.document is not None
    assert item.document.blocks[0].table == [["Property", "Value"], ["Status", "Doing"], ["Tags", "AP, Pilot"]]
    assert item.author == ""
    await connector.close()


async def test_notion_retries_429_with_retry_after(router, sleeps):
    router.get(f"{NOTION}/pages/{P1}").mock(side_effect=[
        ok({"object": "error", "code": "rate_limited"}, 429, **{"Retry-After": "7"}),
        ok(notion_page(P1, "Intro", page_parent(R))),
    ])
    router.get(f"{NOTION}/blocks/{P1}/children").mock(return_value=ok({"results": [], "has_more": False}))
    router.get(f"{NOTION}/users/u1").mock(return_value=ok({"name": "Asha"}))
    connector = NotionConnector({"root_page_id": R})
    item = await connector.fetch(Change(external_id=P1))
    assert item is not None and item.title == "Intro"
    assert 7.0 in sleeps
    await connector.close()


async def test_notion_audience_is_unknown():
    audience = await NotionConnector({}).source_audience()
    assert audience.unknown


# ---------------------------------------------------------------------------
# Granola

GRANOLA = "https://public-api.granola.ai/v1"


def granola_note(nid: str, updated: str, deleted: str | None = None) -> dict[str, Any]:
    return {"id": nid, "object": "note", "title": f"Meeting {nid}", "owner": {"name": "Asha", "email": "asha@example.org"},
            "created_at": "2026-10-01T09:00:00Z", "updated_at": updated, "deleted_at": deleted}


def granola_lists(router: respx.MockRouter, notes_by_scope: dict[str, list[list[dict[str, Any]]]]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def notes(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer grn_secret"
        params = request.url.params
        scope = params.get("space_id") or params.get("folder_id")
        pages = notes_by_scope.get(scope, [[]])
        index = int(params.get("cursor") or 0)
        more = index + 1 < len(pages)
        return ok({"notes": pages[index], "hasMore": more, "cursor": str(index + 1) if more else None})

    router.get(f"{GRANOLA}/notes").mock(side_effect=notes)
    router.get(f"{GRANOLA}/spaces").mock(return_value=ok(
        {"spaces": [{"id": "spa_A", "object": "space", "name": "Programme"}], "hasMore": False, "cursor": None}))
    router.get(f"{GRANOLA}/folders").mock(return_value=ok({"folders": [
        {"id": "fol_B", "object": "folder", "name": "Leadership", "parent_folder_id": None, "space_id": None},
        {"id": "fol_C", "object": "folder", "name": "Weekly", "parent_folder_id": "fol_B", "space_id": None},
    ], "hasMore": False, "cursor": None}))
    return seen


async def test_granola_lists_only_allowlisted_scopes(router):
    seen = granola_lists(router, {
        "spa_A": [[granola_note("not_1", "2026-10-10T10:00:00Z")], [granola_note("not_2", "2026-10-11T10:00:00Z")]],
        "fol_B": [[granola_note("not_2", "2026-10-11T10:00:00Z"), granola_note("not_3", "2026-10-12T10:00:00Z", deleted="2026-10-12T10:00:00Z")]],
    })
    connector = GranolaConnector({"space_ids": ["spa_A"], "folder_ids": ["fol_B"]})
    batch = await connector.list_changes(None)
    by_id = {c.external_id: c for c in batch.changes}
    assert set(by_id) == {"not_1", "not_2", "not_3"}
    assert by_id["not_1"].path == ["Programme"] and by_id["not_3"].path == ["Leadership"]
    assert by_id["not_3"].deleted and not by_id["not_2"].deleted
    assert all("updated_after" not in r.url.params for r in seen)
    hwm = json.loads(batch.next_cursor)["hwm"]
    assert hwm.startswith("2026-10-12T10:00")

    seen.clear()
    await connector.list_changes(batch.next_cursor)
    assert {r.url.params["updated_after"] for r in seen} == {"2026-10-12T09:58:00Z"}
    await connector.close()


async def test_granola_requires_an_allowlist():
    with pytest.raises(ValueError):
        await GranolaConnector({}).list_changes(None)


def transcript_item(who: dict[str, Any], text: str, start: str) -> dict[str, Any]:
    return {"speaker": {"source": "microphone", **who}, "text": text, "start_time": start, "end_time": start}


FULL_NOTE = {
    **granola_note("not_1", "2026-10-10T10:00:00Z"),
    "web_url": "https://notes.granola.ai/d/abc",
    "calendar_event": {"event_title": "AP review", "invitees": [{"email": "ravi@example.org"}, {"email": "asha@example.org"}],
                       "organiser": "asha@example.org", "calendar_event_id": "e1",
                       "scheduled_start_time": "2026-10-01T09:00:00Z", "scheduled_end_time": "2026-10-01T10:00:00Z"},
    "attendees": [{"name": "Asha", "email": "asha@example.org"}],
    "folder_membership": [{"id": "fol_X", "object": "folder", "name": "AP", "parent_folder_id": None, "space_id": "spa_A"}],
    "space_membership": [{"id": "spa_A", "object": "space", "name": "Programme"}],
    "summary_text": "", "summary_markdown": "## Decisions\n- Ship the pilot\n- Hire two\n\nAll agreed.",
    "private_notes_text": None, "private_notes_markdown": None,
    "transcript": [
        transcript_item({"attribution": "me"}, "Hello there.", "2026-10-01T09:00:02Z"),
        transcript_item({"attribution": "me"}, "How are you?", "2026-10-01T09:00:05Z"),
        transcript_item({"attribution": "them"}, "Fine.", "2026-10-01T09:00:09Z"),
        transcript_item({"name": "Ravi"}, "Numbers are up.", "2026-10-01T10:01:02Z"),
    ],
}


async def test_granola_note_to_document_with_speaker_turns(router):
    route = router.get(f"{GRANOLA}/notes/not_1").mock(return_value=ok(FULL_NOTE))
    connector = GranolaConnector({"space_ids": ["spa_A"]})
    item = await connector.fetch(Change(external_id="not_1", path=["Programme"]))
    assert item is not None and item.document is not None
    assert route.calls[0].request.url.params["include"] == "transcript"
    doc = item.document
    assert item.kind == "meeting" and doc.kind == "meeting" and item.url == "https://notes.granola.ai/d/abc"
    assert doc.metadata["meeting_date"] == "2026-10-01T09:00:00Z"
    assert doc.metadata["attendees"] == ["Asha <asha@example.org>", "ravi@example.org"]
    assert doc.metadata["space"] == "Programme" and doc.metadata["folder"] == "AP"
    assert [(b.kind, b.text) for b in doc.blocks[:3]] == [
        ("heading", "Decisions"), ("list", "- Ship the pilot\n- Hire two"), ("paragraph", "All agreed.")]
    turns = [b for b in doc.blocks if b.kind == "speaker_turn"]
    assert [(b.text, b.locator) for b in turns] == [
        ("Asha: Hello there. How are you?", {"t": "00:00:00"}),
        ("Participant: Fine.", {"t": "00:00:07"}),
        ("Ravi: Numbers are up.", {"t": "01:01:00"}),
    ]
    await connector.close()


async def test_granola_note_is_for_the_people_in_the_meeting(router):
    router.get(f"{GRANOLA}/notes/not_1").mock(return_value=ok(FULL_NOTE))
    connector = GranolaConnector({"space_ids": ["spa_A"]})
    item = await connector.fetch(Change(external_id="not_1", path=["Programme"]))
    assert item.extra["audience"] == {"emails": ["asha@example.org", "ravi@example.org"],
                                      "groups": [], "domains": [], "public": False, "unknown": False}
    await connector.close()


async def test_granola_large_transcript_is_paged(router):
    note = {**FULL_NOTE, "id": "not_2", "transcript": None}
    router.get(f"{GRANOLA}/notes/not_2", params={"include": "transcript"}).mock(
        return_value=ok({"code": "TRANSCRIPT_TOO_LARGE"}, 413))
    router.get(f"{GRANOLA}/notes/not_2").mock(return_value=ok(note))
    pages = [
        {"transcript": [{"speaker": {"source": "speaker", "diarization_label": "Speaker A"}, "text": "Part one."}],
         "hasMore": True, "cursor": "next"},
        {"transcript": [{"speaker": {"source": "speaker", "name": "Ravi"}, "text": "Part two."}],
         "hasMore": False, "cursor": None},
    ]
    router.get(f"{GRANOLA}/notes/not_2/transcript").mock(side_effect=[ok(p) for p in pages])
    connector = GranolaConnector({"folder_ids": ["fol_X"]})
    connector._folders = {}
    item = await connector.fetch(Change(external_id="not_2"))
    assert item is not None and item.document is not None
    turns = [(b.text, b.locator) for b in item.document.blocks if b.kind == "speaker_turn"]
    assert turns == [("Speaker A: Part one.", {}), ("Ravi: Part two.", {})]
    await connector.close()


async def test_granola_skips_note_outside_allowlist(router):
    router.get(f"{GRANOLA}/notes/not_1").mock(return_value=ok(FULL_NOTE))
    granola_lists(router, {})
    connector = GranolaConnector({"folder_ids": ["fol_B"]})
    assert await connector.fetch(Change(external_id="not_1")) is None
    # ...but a note in a child folder of an allowed folder is fine
    nested = {**FULL_NOTE, "space_membership": [], "folder_membership": [
        {"id": "fol_C", "object": "folder", "name": "Weekly", "parent_folder_id": "fol_B", "space_id": None}]}
    router.get(f"{GRANOLA}/notes/not_9").mock(return_value=ok({**nested, "id": "not_9"}))
    assert await connector.fetch(Change(external_id="not_9")) is not None
    await connector.close()


async def test_granola_backs_off_on_429_then_gives_up(router, sleeps):
    router.get(f"{GRANOLA}/notes/not_1").mock(side_effect=[ok({}, 429, **{"Retry-After": "3"}), ok(FULL_NOTE)])
    connector = GranolaConnector({"space_ids": ["spa_A"]})
    assert await connector.fetch(Change(external_id="not_1")) is not None
    assert 3.0 in sleeps

    router.get(f"{GRANOLA}/spaces").mock(return_value=ok({}, 503))
    with pytest.raises(SourceError):
        await connector.list_changes(None)
    assert router.routes[-1].call_count == 1 + _http.MAX_RETRIES
    await connector.close()


# ---------------------------------------------------------------------------
# Registry


def test_registry():
    assert isinstance(get_connector("gdrive", {"drive_id": "d"}), GoogleDriveConnector)
    assert isinstance(get_connector("notion", {}), NotionConnector)
    assert isinstance(get_connector("granola", {"space_ids": ["spa_A"]}), GranolaConnector)
    with pytest.raises(ValueError):
        get_connector("dropbox", {})
    with pytest.raises(ValueError):
        get_connector("gdrive", {})


async def test_drive_probe_reports_role_sharing_and_discovery(router, fake_creds, capsys):
    from nebula import drive_probe

    drive = FakeDrive(router)
    drive.files["fold0000001"] = drive_file("fold0000001", "Nebula Test", FOLDER, "x", permissionIds=["p1"],
                                           capabilities={"canEdit": False, "canShare": False})
    drive.lists["'fold0000001' in parents and trashed = false"] = [[
        drive_file("doc1", "Field Story", DOC, "fold0000001", permissionIds=["p1"])]]
    drive.permissions["doc1"] = [{"type": "user", "role": "reader", "emailAddress": "a@example.org"}]
    drive.lists["sharedWithMe and trashed = false"] = [[drive_file("fold0000001", "Nebula Test", FOLDER, "x")]]
    router.get("https://www.googleapis.com/drive/v3/drives").mock(return_value=ok({"drives": []}))
    await drive_probe.main("https://drive.google.com/drive/folders/fold0000001")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["role"] == "Viewer" and lines[0]["folder_permission_ids_visible"]
    assert lines[1] == {"check": "file sharing", "file": "Field Story", "permission_ids_on_listing": True,
                        "permissions_list": "readable", "people": 1, "groups": 0, "domains": []}
    assert lines[2]["shared_with_nebula"] == 1
