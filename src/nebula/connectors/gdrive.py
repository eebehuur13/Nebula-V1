"""Google Drive connector: everything shared with Nebula's service account, read with that account.

config: {"roots": [{"id": "...", "kind": "folder" | "file" | "drive"}, ...]}, the shared folders,
single files and Shared Drives to read. The older single-root forms {"drive_id": "..."} and
{"folder_id": "..."} still work.

The cursor is compact JSON holding one Drive changes token (the service account's own change feed
covers every root, including Shared Drives) and the folder map ({folder id: [name, parent id,
limited-access flag, shared drive id, sharing fingerprint]}). The folder map is what lets us build
paths, spot limited-access folders and decide whether a change is under a root without asking
Drive about every parent.

A full listing (cursor=None) can span several calls: the batch says has_more and next_cursor
carries the listing position. The changes token is taken before the listing starts, so edits made
while we list are picked up afterwards. Roots added later are walked on the next call, while the
change feed keeps covering the others.

Each change carries the file's revision marker (modified time + checksum) and a fingerprint of its
sharing (`permissionIds`), so the worker only downloads files whose content changed and only
re-reads sharing that changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import google.auth.exceptions
import google.auth.transport
import httpx
from google.oauth2 import service_account

from nebula.config import settings
from nebula.connectors._http import RETRY_STATUSES, SourceError, parse_time, send_with_retry
from nebula.connectors.base import Change, ChangeBatch, Connector, FetchedItem, SourceAudience

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER = "application/vnd.google-apps.folder"
MAX_DOWNLOAD = 50 * 1024 * 1024
BATCH_TARGET = 1000      # changes per batch during a walk (soft limit)
MAX_DEPTH = 100          # folder nesting guard
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"}
ALL_DRIVES = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
ROOT_KINDS = ("folder", "file", "drive")

FILE_FIELDS = (
    "id,name,mimeType,modifiedTime,createdTime,parents,webViewLink,size,md5Checksum,version,"
    "trashed,driveId,inheritedPermissionsDisabled,permissionIds,owners(displayName,emailAddress),"
    "lastModifyingUser(displayName,emailAddress),capabilities(canDownload)"
)
FOLDER_FIELDS = "id,name,mimeType,parents,trashed,driveId,inheritedPermissionsDisabled,permissionIds"
SHARED_FIELDS = "id,name,mimeType,size,driveId,parents,sharingUser(emailAddress,displayName),capabilities(canEdit)"
DESCRIBE_FIELDS = "id,name,mimeType,size,driveId,parents,trashed,webViewLink,capabilities(canEdit)"

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Google-native types we export: source mime -> (export mime, extension, kind)
GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": ("text/markdown", ".md", "doc"),
    "application/vnd.google-apps.spreadsheet": (XLSX, ".xlsx", "sheet"),
    "application/vnd.google-apps.presentation": (PPTX, ".pptx", "slides"),
}
# Uploaded files we download, by extension.
EXT_KIND = {
    ".pdf": "pdf", ".docx": "doc", ".pptx": "slides",
    ".xlsx": "sheet", ".xlsm": "sheet", ".csv": "sheet", ".tsv": "sheet",
    ".txt": "file", ".md": "file", ".html": "file", ".json": "file",
}
MIME_EXT = {
    "application/pdf": ".pdf", DOCX: ".docx", PPTX: ".pptx", XLSX: ".xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": ".xlsm",
    "text/csv": ".csv", "text/tab-separated-values": ".tsv", "text/plain": ".txt",
    "text/markdown": ".md", "text/x-markdown": ".md", "text/html": ".html", "application/json": ".json",
}


# ---------------------------------------------------------------------------
# What we index


@dataclass(frozen=True)
class Plan:
    """How to fetch one file: export (Google-native) or download, and what it becomes."""

    kind: str
    ext: str
    export_mime: str | None = None


def classify(file: dict[str, Any]) -> Plan | None:
    """The fetch plan for a Drive file, or None if Nebula doesn't index it."""
    mime = file.get("mimeType", "")
    if mime in GOOGLE_EXPORTS:
        export_mime, ext, kind = GOOGLE_EXPORTS[mime]
        return Plan(kind, ext, export_mime)
    if mime.startswith("application/vnd.google-apps."):
        return None  # folders, shortcuts, forms, drawings, sites, ...
    ext = PurePosixPath(file.get("name", "")).suffix.lower()
    if ext not in EXT_KIND:
        ext = MIME_EXT.get(mime.lower(), "")
    if not ext:
        return None
    if int(file.get("size") or 0) > MAX_DOWNLOAD:
        return None
    return Plan(EXT_KIND[ext], ext)


def filename_for(file: dict[str, Any], plan: Plan) -> str:
    """A filename with the right extension, safe to hand to the parsers."""
    name = (file.get("name") or file.get("id", "file")).replace("/", "-")
    return name if name.lower().endswith(plan.ext) else name + plan.ext


def can_edit(resource: dict[str, Any]) -> bool | None:
    """Whether Nebula has Editor access, which Drive requires before it shows who a file is shared
    with (tested live on 20 Oct: as Viewer, permissionIds and the sharing list were unreadable).
    None when Drive didn't say."""
    value = (resource.get("capabilities") or {}).get("canEdit")
    return value if isinstance(value, bool) else None


def acl_fingerprint(file: dict[str, Any]) -> str | None:
    """Who the file is shared with, as Drive's permission ids; None when Drive didn't say."""
    ids = file.get("permissionIds")
    return ",".join(sorted(ids)) if isinstance(ids, list) else None


def revision(file: dict[str, Any]) -> str:
    """Changes whenever the content does. Empty when Drive gave no modified time (always fetch)."""
    modified = file.get("modifiedTime") or ""
    return f"{modified}|{file.get('md5Checksum') or ''}" if modified else ""


def _folder_entry(file: dict[str, Any]) -> list[Any]:
    return [file.get("name", ""), (file.get("parents") or [""])[0], int(bool(file.get("inheritedPermissionsDisabled"))),
            file.get("driveId") or "", acl_fingerprint(file) or ""]


def _entry_acl(entry: list[Any]) -> str:
    return entry[4] if len(entry) > 4 else ""


def _entry_drive(entry: list[Any] | None) -> str | None:
    return (entry[3] if entry and len(entry) > 3 else "") or None


def _parent(file: dict[str, Any]) -> str:
    return (file.get("parents") or [""])[0]


def _children_query(folder_id: str, include_trashed: bool = False) -> str:
    query = f"'{folder_id}' in parents"
    return query if include_trashed else query + " and trashed = false"


def _error_reasons(response: httpx.Response) -> set[str]:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return set()
    if not isinstance(error, dict):
        return set()
    items = [*error.get("errors", []), *error.get("details", [])]
    return {str(item.get("reason", "")) for item in items if isinstance(item, dict)}


def _drive_retryable(response: httpx.Response) -> bool:
    if response.status_code in RETRY_STATUSES:
        return True
    return response.status_code == 403 and bool(_error_reasons(response) & RATE_LIMIT_REASONS)


# ---------------------------------------------------------------------------
# Cursor


@dataclass
class _State:
    roots: dict[str, str]                  # root id -> "folder" | "file" | "drive", as walked
    folders: dict[str, list[Any]]          # folder id -> [name, parent id, limited 0/1, drive id, acl]
    token: str = ""                        # Drive changes page token
    queue: list[str] = field(default_factory=list)          # folders still to walk
    pending: list[str] = field(default_factory=list)        # file roots still to list
    drives: list[list[str]] = field(default_factory=list)   # [shared drive id, files.list page token]
    full: bool = False                     # a full listing is in progress

    def dumps(self) -> str:
        data = {"v": 2, "roots": self.roots, "token": self.token, "queue": self.queue, "pending": self.pending,
                "drives": self.drives, "full": self.full, "folders": self.folders}
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def loads(cls, cursor: str) -> _State | None:
        """The saved state, or None for a cursor from an older version (it gets a fresh full listing)."""
        try:
            d = json.loads(cursor)
        except ValueError:
            return None
        if not isinstance(d, dict) or d.get("v") != 2:
            return None
        return cls(roots=d.get("roots", {}), folders=d.get("folders", {}), token=d.get("token", ""),
                   queue=d.get("queue", []), pending=d.get("pending", []), drives=d.get("drives", []),
                   full=bool(d.get("full")))


# ---------------------------------------------------------------------------
# Service-account auth (google-auth mints tokens; httpx does the HTTP)


class _AuthResponse(google.auth.transport.Response):
    def __init__(self, response: httpx.Response):
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._response.headers)

    @property
    def data(self) -> bytes:
        return self._response.content


class _AuthRequest(google.auth.transport.Request):
    """google-auth transport over a sync httpx client, so we don't need `requests`."""

    def __init__(self, client: httpx.Client):
        self._client = client

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):  # type: ignore[override]
        try:
            response = self._client.request(method, url, content=body, headers=headers, timeout=timeout or 30)
        except httpx.HTTPError as exc:
            raise google.auth.exceptions.TransportError(str(exc)) from exc
        return _AuthResponse(response)


_credential_cache: dict[tuple[bool, str], Any] = {}


def load_credentials(*, write: bool = False) -> Any:
    """Keyless AWS workload federation (or a legacy service-account key), shared per process so
    frequent syncs don't mint a new token every time."""
    s = settings()
    if not s.google_service_account_json:
        raise ValueError("NEBULA_GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    key = (write, hashlib.sha256((s.google_service_account_json + s.google_impersonate_email).encode()).hexdigest())
    if key in _credential_cache:
        return _credential_cache[key]
    info = json.loads(s.google_service_account_json)
    scopes = ["https://www.googleapis.com/auth/drive" if write else SCOPE]
    if info.get("type") == "external_account":
        from nebula.connectors.aws_google import federated_credentials

        if s.google_impersonate_email:
            raise ValueError("Domain-wide user impersonation is not supported with workload federation.")
        creds = federated_credentials(info, region=s.aws_region, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=scopes, subject=s.google_impersonate_email or None
        )
    _credential_cache[key] = creds
    return creds


def _refresh(credentials: Any) -> None:
    with httpx.Client() as client:
        credentials.refresh(_AuthRequest(client))


# ---------------------------------------------------------------------------
# Connector


class GoogleDriveConnector(Connector):
    kind = "gdrive"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.drive_id: str = config.get("drive_id") or ""
        self.folder_id: str = config.get("folder_id") or ""
        roots = config.get("roots")
        if roots is None:
            if bool(self.drive_id) == bool(self.folder_id):
                raise ValueError("gdrive config needs roots, or exactly one of drive_id or folder_id")
            roots = [{"id": self.drive_id or self.folder_id, "kind": "drive" if self.drive_id else "folder"}]
        self.roots: dict[str, str] = {}
        for r in roots:
            if r.get("kind", "folder") not in ROOT_KINDS:
                raise ValueError(f"unknown Drive root kind {r.get('kind')!r}")
            self.roots[r["id"]] = r.get("kind", "folder")
        # Roots to walk again on this sync (to re-check everything under them).
        self._rewalk: list[str] = [r for r in config.get("rewalk") or [] if r in self.roots]
        self._client: httpx.AsyncClient | None = None
        self._credentials: Any = None
        self._auth_lock = asyncio.Lock()
        self._outside: set[str] = set()   # folders seen this call that are not under a root
        self._learned: set[str] = set()   # folders added to the map this call by resolving parents

    # -- HTTP -----------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=API, timeout=httpx.Timeout(60.0, connect=15.0))
        return self._client

    async def _token(self, force: bool = False) -> str:
        async with self._auth_lock:
            if self._credentials is None:
                self._credentials = load_credentials()
            if force or not self._credentials.valid:
                await asyncio.to_thread(_refresh, self._credentials)
            return self._credentials.token

    async def _send(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with retries; refreshes the token once on 401."""
        response: httpx.Response | None = None
        for attempt in range(2):
            token = await self._token(force=attempt > 0)
            response = await send_with_retry(
                self._http(), "GET", path, params=params,
                headers={"Authorization": f"Bearer {token}"}, retry_if=_drive_retryable,
            )
            if response.status_code != 401:
                break
        assert response is not None
        return response

    async def _get_json(
        self, path: str, params: dict[str, Any] | None = None, *, missing_ok: bool = False
    ) -> dict[str, Any] | None:
        response = await self._send(path, params)
        if missing_ok and (response.status_code == 404 or (response.status_code == 403 and not _drive_retryable(response))):
            return None
        if response.is_error:
            raise SourceError("Drive", response)
        return response.json()

    async def _iter_files(self, query: str, drive_id: str | None) -> AsyncIterator[dict[str, Any]]:
        page = ""
        while True:
            files, page = await self._list_files(query, drive_id, page)
            for file in files:
                yield file
            if not page:
                return

    async def _list_files(self, query: str, drive_id: str | None, page: str = "",
                          fields: str = FILE_FIELDS) -> tuple[list[dict[str, Any]], str]:
        params: dict[str, Any] = {"q": query, "pageSize": 1000, "fields": f"nextPageToken,files({fields})", **ALL_DRIVES}
        if drive_id:
            params |= {"corpora": "drive", "driveId": drive_id}
        if page:
            params["pageToken"] = page
        data = await self._get_json("/files", params) or {}
        return data.get("files", []), data.get("nextPageToken", "")

    async def _start_token(self, drive_id: str | None = None) -> str:
        params: dict[str, Any] = {"supportsAllDrives": "true"}
        if drive_id:
            params["driveId"] = drive_id
        data = await self._get_json("/changes/startPageToken", params) or {}
        return data["startPageToken"]

    # -- Listing ----------------------------------------------------------------

    async def list_changes(self, cursor: str | None) -> ChangeBatch:
        self._outside.clear()
        self._learned.clear()
        lost: list[str] = []
        state = _State.loads(cursor) if cursor else None
        if state is None:
            state = await self._start_full_listing(lost)
        else:
            await self._sync_roots(state, lost)
        if self._rewalk:
            for rid in self._rewalk:
                await self._requeue_root(state, rid)
            self._rewalk = []
        if state.pending or state.drives or state.queue:
            out: dict[str, Change] = {}
            await self._walk_some(state, out, lost)
            if state.pending or state.drives or state.queue:
                return ChangeBatch(list(out.values()), state.dumps(), has_more=True, lost_roots=lost)
            was_full, state.full = state.full, False
            # After a full listing stop here: the change feed is read next time. After walking roots
            # that were just added, carry on with the change feed in this same sync.
            return ChangeBatch(list(out.values()), state.dumps(), has_more=not was_full, lost_roots=lost)
        batch = await self._list_incremental(state)
        batch.lost_roots = lost + batch.lost_roots
        return batch

    async def _start_full_listing(self, lost: list[str]) -> _State:
        state = _State(roots={}, folders={}, token=await self._start_token(), full=True)
        for rid, kind in self.roots.items():
            await self._add_root(state, rid, kind, lost)
        return state

    async def _sync_roots(self, state: _State, lost: list[str]) -> None:
        """Walk roots added since the last sync; forget roots that were removed."""
        for rid, kind in self.roots.items():
            if rid not in state.roots:
                await self._add_root(state, rid, kind, lost)
        for rid in [r for r in state.roots if r not in self.roots]:
            del state.roots[rid]
            state.pending = [p for p in state.pending if p != rid]
            state.drives = [d for d in state.drives if d[0] != rid]
            # Its folders stay in the map: they may sit under another root.

    async def _add_root(self, state: _State, rid: str, kind: str, lost: list[str]) -> None:
        if kind == "file":
            state.roots[rid] = "file"
            state.pending.append(rid)
            return
        if kind == "drive":
            drive = await self._get_json(f"/drives/{rid}", {"fields": "id,name"}, missing_ok=True)
            if drive is None:
                lost.append(rid)
                return
            state.roots[rid] = "drive"
            state.folders[rid] = [drive.get("name", ""), "", 0, rid, ""]
            # Folders first, so every file's path can be built from the map.
            async for folder in self._iter_files(f"mimeType = '{FOLDER}' and trashed = false", rid):
                state.folders[folder["id"]] = _folder_entry(folder)
            state.drives.append([rid, ""])
            return
        entry = await self._fetch_folder(rid)
        if entry is None:
            lost.append(rid)
            return
        state.roots[rid] = "folder"
        state.folders[rid] = entry
        state.queue.append(rid)

    async def _requeue_root(self, state: _State, rid: str) -> None:
        kind = state.roots.get(rid)
        if kind == "file" and rid not in state.pending:
            state.pending.append(rid)
        elif kind == "drive" and not any(d[0] == rid for d in state.drives):
            state.drives.append([rid, ""])
        elif kind == "folder" and rid not in state.queue:
            state.queue.append(rid)

    async def _walk_some(self, state: _State, out: dict[str, Change], lost: list[str]) -> None:
        """Walk part of what's waiting: file roots, then Shared Drive pages, then folders."""
        while (state.pending or state.drives or state.queue) and len(out) < BATCH_TARGET:
            if state.pending:
                rid = state.pending.pop(0)
                file = await self._get_json(f"/files/{rid}", {"fields": FILE_FIELDS, "supportsAllDrives": "true"},
                                            missing_ok=True)
                if not file or file.get("trashed"):
                    lost.append(rid)
                elif change := await self._upsert_change(state, file):
                    out[rid] = change
                continue
            if state.drives:
                drive_id, page = state.drives[0]
                files, next_page = await self._list_files(f"mimeType != '{FOLDER}' and trashed = false", drive_id, page)
                for file in files:
                    if change := await self._upsert_change(state, file):
                        out[change.external_id] = change
                if next_page:
                    state.drives[0][1] = next_page
                else:
                    state.drives.pop(0)
                return  # one page of a Shared Drive per call, so progress is saved between pages
            await self._walk_folder(state, state.queue.pop(0), out, state.queue)

    async def _walk_folder(self, state: _State, folder_id: str, out: dict[str, Change], queue: list[str],
                           recheck: bool = False) -> None:
        """List one folder: register subfolders (queued for walking) and emit its files."""
        drive = _entry_drive(state.folders.get(folder_id))
        async for file in self._iter_files(_children_query(folder_id), drive):
            if file.get("mimeType") == FOLDER:
                state.folders[file["id"]] = _folder_entry(file)
                queue.append(file["id"])
            elif change := await self._upsert_change(state, file):
                change.recheck_acl = recheck
                out[change.external_id] = change

    async def _relist_subtree(self, state: _State, folder_id: str, out: dict[str, Change], recheck: bool = False) -> None:
        queue = [folder_id]
        while queue:
            await self._walk_folder(state, queue.pop(0), out, queue, recheck)

    async def _upsert_change(self, state: _State, file: dict[str, Any]) -> Change | None:
        """A Change for an indexable file under a root (or a root itself), else None."""
        if classify(file) is None:
            return None
        if state.roots.get(file["id"]) == "file":
            root, chain = file["id"], []
        else:
            chain = await self._chain(state, _parent(file))
            if chain is None:
                return None
            root = chain[0][0]
        narrower = bool(chain) and (bool(file.get("inheritedPermissionsDisabled")) or any(e[2] for _, e in chain[1:]))
        return Change(
            external_id=file["id"],
            title=file.get("name", ""),
            url=file.get("webViewLink", ""),
            mime_type=file.get("mimeType", ""),
            modified_at=parse_time(file.get("modifiedTime")),
            path=[entry[0] for _, entry in chain],
            parent_external_id=_parent(file) or None,
            narrower_than_root=narrower,
            root=root,
            rev=revision(file),
            acl=acl_fingerprint(file),
        )

    async def _chain(self, state: _State, folder_id: str) -> list[tuple[str, list[Any]]] | None:
        """(id, entry) from the nearest root down to `folder_id`, or None if it isn't under a root."""
        walked: list[tuple[str, list[Any]]] = []
        current = folder_id
        while current and len(walked) < MAX_DEPTH and current not in self._outside:
            entry = state.folders.get(current) or await self._fetch_folder(current)
            if entry is None:
                break
            walked.append((current, entry))
            if state.roots.get(current) in ("folder", "drive"):
                for fid, found in walked:
                    if fid not in state.folders:
                        state.folders[fid] = found
                        self._learned.add(fid)
                return list(reversed(walked))
            current = entry[1]
        self._outside.update(fid for fid, _ in walked if fid not in state.folders)
        if current:
            self._outside.add(current)
        return None

    async def _fetch_folder(self, folder_id: str) -> list[Any] | None:
        params = {"fields": FOLDER_FIELDS, "supportsAllDrives": "true"}
        folder = await self._get_json(f"/files/{folder_id}", params, missing_ok=True)
        if not folder or folder.get("trashed") or folder.get("mimeType") != FOLDER:
            return None
        return _folder_entry(folder)

    # -- Incremental --------------------------------------------------------------

    async def _list_incremental(self, state: _State) -> ChangeBatch:
        params: dict[str, Any] = {
            "pageToken": state.token, "pageSize": 1000, "includeRemoved": "true", **ALL_DRIVES,
            "fields": f"nextPageToken,newStartPageToken,changes(changeType,removed,fileId,file({FILE_FIELDS}))",
        }
        data = await self._get_json("/changes", params) or {}
        out: dict[str, Change] = {}
        lost: list[str] = []
        for item in data.get("changes", []):
            await self._apply_change(state, item, out, lost)
        has_more = bool(data.get("nextPageToken"))
        state.token = data.get("nextPageToken") or data.get("newStartPageToken") or state.token
        return ChangeBatch(changes=list(out.values()), next_cursor=state.dumps(), has_more=has_more, lost_roots=lost)

    async def _apply_change(self, state: _State, item: dict[str, Any], out: dict[str, Change], lost: list[str]) -> None:
        file_id = item.get("fileId")
        file = item.get("file")
        if item.get("changeType") == "drive" or not file_id:
            return
        gone = bool(item.get("removed")) or not file or bool(file.get("trashed"))
        kind = state.roots.get(file_id)
        if kind:
            await self._root_changed(state, file_id, kind, file, gone, out, lost)
        elif gone:
            await self._drop(state, file_id, out)
        elif file.get("mimeType") == FOLDER:
            await self._folder_changed(state, file, out)
        else:
            # Not under a root (any more) or no longer indexable -> deletion (a no-op if never indexed).
            change = await self._upsert_change(state, file)
            out[file_id] = change or Change(external_id=file_id, deleted=True, title=file.get("name", ""))

    async def _root_changed(self, state: _State, rid: str, kind: str, file: dict[str, Any] | None, gone: bool,
                            out: dict[str, Change], lost: list[str]) -> None:
        if gone:
            # Unshared, trashed or deleted: Nebula can't read it any more.
            lost.append(rid)
            await self._drop(state, rid, out)
            return
        assert file is not None
        if kind == "file":
            change = await self._upsert_change(state, file)
            out[rid] = change or Change(external_id=rid, deleted=True, title=file.get("name", ""))
            return
        old = state.folders.get(rid)
        new = _folder_entry(file)
        if kind == "drive" and old:
            new[0] = old[0]  # a Shared Drive's root folder doesn't carry the drive's name
        state.folders[rid] = new
        if old and _entry_acl(old) != _entry_acl(new):
            # Sharing on the root changed, so sharing on everything below it may have too.
            await self._relist_subtree(state, rid, out, recheck=True)

    async def _folder_changed(self, state: _State, folder: dict[str, Any], out: dict[str, Change]) -> None:
        folder_id = folder["id"]
        old = state.folders.get(folder_id)
        new = _folder_entry(folder)
        if await self._chain(state, new[1]) is None:
            if old is not None:  # moved out of every root
                await self._drop(state, folder_id, out)
            return
        if old == new and folder_id not in self._learned:
            return
        # New, moved, renamed or re-shared: re-list what's below so paths, flags and sharing are current.
        self._learned.discard(folder_id)
        state.folders[folder_id] = new
        await self._relist_subtree(state, folder_id, out, recheck=old is None or _entry_acl(old) != _entry_acl(new))

    async def _drop(self, state: _State, file_id: str, out: dict[str, Change]) -> None:
        """Delete a file, or a folder's whole subtree, from the collection."""
        if file_id not in state.folders:
            out[file_id] = Change(external_id=file_id, deleted=True)
            return
        async for file in self._descendant_files(state, file_id):
            out[file["id"]] = Change(external_id=file["id"], deleted=True, title=file.get("name", ""))
        _forget_folder(state.folders, file_id)

    async def _descendant_files(self, state: _State, folder_id: str) -> AsyncIterator[dict[str, Any]]:
        """Every non-folder below a folder, trashed or not. Stops quietly where access is gone."""
        queue = [folder_id]
        while queue:
            current = queue.pop(0)
            try:
                async for file in self._iter_files(_children_query(current, include_trashed=True),
                                                   _entry_drive(state.folders.get(current))):
                    if file.get("mimeType") == FOLDER:
                        queue.append(file["id"])
                    else:
                        yield file
            except SourceError as exc:
                log.info("can't list removed folder %s (%s); reconciliation will catch the rest", current, exc.status_code)

    # -- One file, and what's shared with Nebula --------------------------------

    async def change_for(self, file_id: str, cursor: str | None = None) -> Change | None:
        """A Change for one file if it's readable and under a root (or a root itself), else None.

        Used to refresh a single file on request. The saved cursor's folder map saves lookups;
        nothing is written back to it.
        """
        state = (_State.loads(cursor) if cursor else None) or _State(roots={}, folders={})
        state.roots = dict(self.roots)
        file = await self._get_json(f"/files/{file_id}", {"fields": FILE_FIELDS, "supportsAllDrives": "true"},
                                    missing_ok=True)
        if not file or file.get("trashed") or file.get("mimeType") == FOLDER:
            return None
        return await self._upsert_change(state, file)

    async def covering_root(self, meta: dict[str, Any], cursor: str | None = None) -> str | None:
        """The root a file or folder (as `describe` returned it) already sits under, if any."""
        state = (_State.loads(cursor) if cursor else None) or _State(roots={}, folders={})
        state.roots = dict(self.roots)
        if meta["id"] in state.roots:
            return meta["id"]
        chain = await self._chain(state, _parent(meta))
        return chain[0][0] if chain else None

    async def describe(self, file_id: str) -> dict[str, Any] | None:
        """Name, type, parents and Shared Drive of something, or None if the service account can't see it."""
        meta = await self._get_json(f"/files/{file_id}", {"fields": DESCRIBE_FIELDS, "supportsAllDrives": "true"},
                                    missing_ok=True)
        if meta is None or meta.get("trashed"):
            return None
        return meta

    async def shared_with_nebula(self) -> list[dict[str, Any]]:
        """Folders and indexable files shared directly with the service account, and the Shared
        Drives it belongs to: what discovery turns into roots."""
        found: list[dict[str, Any]] = []
        page = ""
        while True:
            files, page = await self._list_files("sharedWithMe and trashed = false", None, page, fields=SHARED_FIELDS)
            for f in files:
                if f.get("mimeType") == FOLDER:
                    kind = "folder"
                elif classify(f) is not None:
                    kind = "file"
                else:
                    continue
                sharer = f.get("sharingUser") or {}
                found.append({"id": f["id"], "name": f.get("name", ""), "kind": kind,
                              "shared_by": (sharer.get("emailAddress") or "").lower(), "editor": can_edit(f)})
            if not page:
                break
        page = ""
        while True:
            params: dict[str, Any] = {"pageSize": 100, "fields": "nextPageToken,drives(id,name,capabilities(canEdit))"}
            if page:
                params["pageToken"] = page
            data = await self._get_json("/drives", params) or {}
            for d in data.get("drives", []):
                found.append({"id": d["id"], "name": d.get("name", ""), "kind": "drive", "shared_by": "",
                              "editor": can_edit(d)})
            page = data.get("nextPageToken", "")
            if not page:
                break
        return found

    # -- Fetch -------------------------------------------------------------------

    async def fetch(self, change: Change) -> FetchedItem | None:
        file_id = change.external_id
        file = await self._get_json(f"/files/{file_id}", {"fields": FILE_FIELDS, "supportsAllDrives": "true"}, missing_ok=True)
        if not file or file.get("trashed"):
            return None
        plan = classify(file)
        if plan is None or (file.get("capabilities") or {}).get("canDownload") is False:
            return None
        if plan.export_mime:
            response = await self._send(f"/files/{file_id}/export", {"mimeType": plan.export_mime})
            mime = plan.export_mime
        else:
            response = await self._send(f"/files/{file_id}", {"alt": "media", "supportsAllDrives": "true"})
            mime = file.get("mimeType", "")
        if response.status_code in (403, 404) and not _drive_retryable(response):
            # Gone, or not exportable/downloadable (e.g. exportSizeLimitExceeded at 10 MB).
            log.info("Drive file %s not downloadable (%s %s)", file_id, response.status_code, sorted(_error_reasons(response)))
            return None
        if response.is_error:
            raise SourceError("Drive", response)
        if len(response.content) > MAX_DOWNLOAD:
            return None
        item = self._fetched_item(file, plan, mime, response.content, change.path)
        item.extra["audience"] = await self.file_audience(file_id)
        item.extra["rev"] = revision(file)
        item.extra["acl"] = acl_fingerprint(file)
        return item

    def _fetched_item(self, file: dict[str, Any], plan: Plan, mime: str, data: bytes, path: list[str]) -> FetchedItem:
        owner = (file.get("owners") or [{}])[0]
        modifier = file.get("lastModifyingUser") or {}
        return FetchedItem(
            external_id=file["id"],
            title=file.get("name", ""),
            url=file.get("webViewLink", ""),
            mime_type=mime,
            kind=plan.kind,
            modified_at=parse_time(file.get("modifiedTime")),
            created_at=parse_time(file.get("createdTime")),
            author=owner.get("emailAddress") or modifier.get("emailAddress") or modifier.get("displayName", ""),
            path=list(path),
            data=data,
            filename=filename_for(file, plan),
            extra={
                "source_mime_type": file.get("mimeType", ""),
                "md5": file.get("md5Checksum", ""),
                "version": file.get("version", ""),
                "size": int(file.get("size") or 0),
                "last_modified_by": modifier.get("emailAddress", ""),
            },
        )

    # -- Audience ----------------------------------------------------------------

    async def source_audience(self) -> SourceAudience:
        root = self.drive_id or self.folder_id
        if not root:
            return SourceAudience(unknown=True, note="each file mirrors its own Drive sharing")
        audience = SourceAudience()
        domains: set[str] = set()
        await self._token()  # load credentials so our own service account can be left out
        own_email =(getattr(self._credentials, "service_account_email", "") or "").lower()
        page = ""
        while True:
            params: dict[str, Any] = {
                "supportsAllDrives": "true", "useDomainAdminAccess": "false", "pageSize": 100,
                "fields": "nextPageToken,permissions(type,role,emailAddress,domain,deleted)",
            }
            if page:
                params["pageToken"] = page
            data = await self._get_json(f"/files/{root}/permissions", params, missing_ok=True)
            if data is None:
                return SourceAudience(unknown=True, note="the service account can't list permissions on this root")
            for permission in data.get("permissions", []):
                _add_permission(audience, domains, permission, own_email)
            page = data.get("nextPageToken", "")
            if not page:
                break
        if domains:
            audience.note = "domain-wide access: " + ", ".join(sorted(domains))
        return audience

    async def describe_root(self) -> dict[str, Any] | None:
        """The single root's name and type, or None if the service account can't see it."""
        root = self.drive_id or self.folder_id
        if not root:
            return None
        return await self._get_json(f"/files/{root}", {"fields": "id,name,mimeType,driveId",
                                                         "supportsAllDrives": "true"}, missing_ok=True)

    async def file_audience(self, file_id: str) -> dict[str, Any]:
        """Who can open one file in Drive (direct and inherited permissions), for mirroring into Nebula.

        Returns {"emails", "groups", "domains", "public", "unknown"}; unknown=True when the service
        account isn't allowed to list the file's permissions (then nothing is mirrored).
        """
        audience = SourceAudience()
        domains: set[str] = set()
        await self._token()
        own_email = (getattr(self._credentials, "service_account_email", "") or "").lower()
        page = ""
        while True:
            params: dict[str, Any] = {
                "supportsAllDrives": "true", "pageSize": 100,
                "fields": "nextPageToken,permissions(type,role,emailAddress,domain,deleted)",
            }
            if page:
                params["pageToken"] = page
            data = await self._get_json(f"/files/{file_id}/permissions", params, missing_ok=True)
            if data is None:
                return {"emails": [], "groups": [], "domains": [], "public": False, "unknown": True}
            for permission in data.get("permissions", []):
                _add_permission(audience, domains, permission, own_email)
            page = data.get("nextPageToken", "")
            if not page:
                break
        return {"emails": sorted(audience.emails), "groups": sorted(audience.groups),
                "domains": sorted(d for d in domains if d), "public": audience.public, "unknown": False}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _add_permission(audience: SourceAudience, domains: set[str], permission: dict[str, Any], own_email: str) -> None:
    if permission.get("deleted"):
        return
    kind = permission.get("type")
    email = (permission.get("emailAddress") or "").lower()
    if kind == "user" and email and email != own_email:
        audience.emails.add(email)
    elif kind == "group" and email:
        audience.groups.add(email)
    elif kind == "domain":
        audience.domain_wide = True
        domains.add(permission.get("domain", ""))
    elif kind == "anyone":
        audience.public = True


def _forget_folder(folders: dict[str, list[Any]], folder_id: str) -> None:
    """Remove a folder and every folder below it from the map."""
    doomed = {folder_id}
    grew = True
    while grew:
        grew = False
        for fid, entry in folders.items():
            if fid not in doomed and entry[1] in doomed:
                doomed.add(fid)
                grew = True
    for fid in doomed:
        folders.pop(fid, None)


def service_account_email() -> str:
    """The Google identity Nebula reads Drive as, i.e. who people share folders with."""
    raw = settings().google_service_account_json
    if not raw:
        return ""
    try:
        info = json.loads(raw)
    except ValueError:
        return ""
    if info.get("client_email"):
        return info["client_email"]
    url = info.get("service_account_impersonation_url", "")
    if "/serviceAccounts/" in url:
        return url.split("/serviceAccounts/", 1)[1].split(":", 1)[0]
    return ""


_ID = r"([A-Za-z0-9_-]{10,})"


def drive_id_from(text: str) -> str:
    """The id in a Drive folder, file, Docs/Sheets/Slides or Shared Drive link, or a bare id."""
    text = text.strip()
    for pattern in (rf"/folders/{_ID}", rf"/d/{_ID}", rf"[?&]id={_ID}"):
        if m := re.search(pattern, text):
            return m.group(1)
    if re.fullmatch(_ID, text):
        return text
    raise ValueError("That doesn't look like a Google Drive link or id.")


def folder_id_from(text: str) -> str:
    """Accept a Drive folder link or a bare id."""
    return drive_id_from(text)
