"""Granola connector: meeting notes from an allowlist of spaces and folders.

config: {"space_ids": ["spa_..."], "folder_ids": ["fol_..."]} (at least one id is
required; we never import a whole workspace implicitly). Optional
{"requests_per_second": 4}.

Public API (https://docs.granola.ai): Bearer workspace key, base
https://public-api.granola.ai/v1. `GET /notes` filters by space_id or folder_id
(folders include child folders) and `updated_after`; `GET /notes/{id}?include=transcript`
returns the summary and transcript, or 413 when the transcript is too large, in which
case `GET /notes/{id}/transcript` pages through it. Only notes with a generated summary
and transcript are returned. Deletions show up as `deleted_at` only for keys that see
all workspace notes; otherwise the worker's periodic full listing catches them.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from typing import Any

import httpx

from nebula.config import settings
from nebula.connectors._http import RateLimiter, SourceError, parse_time, send_with_retry
from nebula.connectors.base import Change, ChangeBatch, Connector, FetchedItem, SourceAudience
from nebula.ingest.types import Block, Document

log = logging.getLogger(__name__)

API = "https://public-api.granola.ai/v1"
NOTE_MIME = "application/vnd.granola.note"
SAFETY_WINDOW = timedelta(minutes=2)
PAGE_SIZE = 30            # the API maximum for list endpoints
TRANSCRIPT_PAGE_SIZE = 100
MAX_TURN_CHARS = 1500     # merge consecutive lines from one speaker up to this size


# ---------------------------------------------------------------------------
# Notes -> Document


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def markdown_to_blocks(markdown: str) -> list[Block]:
    """Split simple markdown (headings, lists, quotes, code, paragraphs) into blocks."""
    blocks: list[Block] = []
    buffer: list[str] = []
    buffer_kind = ""
    in_code = False

    def flush() -> None:
        nonlocal buffer, buffer_kind
        text = "\n".join(buffer).strip("\n")
        if text.strip():
            blocks.append(Block(kind=buffer_kind or "paragraph", text=text))  # type: ignore[arg-type]
        buffer, buffer_kind = [], ""

    def add(kind: str, line: str) -> None:
        nonlocal buffer_kind
        if buffer_kind != kind:
            flush()
            buffer_kind = kind
        buffer.append(line)

    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if in_code:
            if stripped.startswith("```"):
                flush()
                in_code = False
            else:
                buffer.append(raw)
            continue
        if stripped.startswith("```"):
            flush()
            buffer_kind, in_code = "code", True
        elif heading := _HEADING.match(stripped):
            flush()
            blocks.append(Block(kind="heading", text=heading.group(2).strip(), level=len(heading.group(1))))
        elif _LIST_ITEM.match(line) or (buffer_kind == "list" and line[:1] in (" ", "\t") and stripped):
            add("list", line)
        elif stripped.startswith(">"):
            add("quote", stripped[1:].strip())
        elif not stripped:
            flush()
        else:
            add("paragraph", stripped)
    flush()
    return blocks


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _speaker(speaker: dict[str, Any], owner_name: str) -> str:
    if speaker.get("name"):
        return speaker["name"]
    if speaker.get("diarization_label"):
        return speaker["diarization_label"]
    if speaker.get("attribution") == "me":
        return owner_name or "Note-taker"
    if speaker.get("attribution") == "them":
        return "Participant"
    return "Speaker"


def transcript_blocks(items: list[dict[str, Any]], owner_name: str) -> list[Block]:
    """Transcript items as speaker turns; consecutive lines from one speaker are merged."""
    starts = [t for t in (parse_time(i.get("start_time")) for i in items) if t]
    origin = min(starts) if starts else None
    turns: list[list[Any]] = []  # [speaker, text, locator]
    for item in items:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        speaker = _speaker(item.get("speaker") or {}, owner_name)
        if turns and turns[-1][0] == speaker and len(turns[-1][1]) + len(text) < MAX_TURN_CHARS:
            turns[-1][1] += " " + text
            continue
        start = parse_time(item.get("start_time"))
        locator = {"t": _clock((start - origin).total_seconds())} if start and origin else {}
        turns.append([speaker, text, locator])
    return [Block(kind="speaker_turn", text=f"{s}: {t}", locator=loc) for s, t, loc in turns]


def _person(user: dict[str, Any]) -> str:
    name, email = user.get("name") or "", user.get("email") or ""
    return f"{name} <{email}>" if name and email else name or email


def note_audience(note: dict[str, Any]) -> dict[str, Any]:
    """Who was in the meeting: the note's owner, its attendees and the calendar invitees.

    Nebula shows each note to them (plus any groups the collection is published to). Granola's API
    doesn't list space members, so "who was there" is the closest thing to the note's own audience.
    """
    found = {(note.get("owner") or {}).get("email") or ""}
    found |= {a.get("email") or "" for a in note.get("attendees") or []}
    found |= {i.get("email") or "" for i in (note.get("calendar_event") or {}).get("invitees") or []}
    emails = sorted({e.strip().lower() for e in found if "@" in e})
    return {"emails": emails, "groups": [], "domains": [], "public": False, "unknown": not emails}


def note_to_document(note: dict[str, Any]) -> Document:
    """A Granola note (with transcript) as a meeting Document."""
    event = note.get("calendar_event") or {}
    owner = note.get("owner") or {}
    title = note.get("title") or event.get("event_title") or "Untitled meeting"
    attendees = [_person(a) for a in note.get("attendees") or [] if _person(a)]
    known = {(a.get("email") or "").lower() for a in note.get("attendees") or []}
    attendees += [i["email"] for i in event.get("invitees") or [] if i.get("email") and i["email"].lower() not in known]

    blocks: list[Block] = []
    if note.get("summary_markdown"):
        blocks += markdown_to_blocks(note["summary_markdown"])
    elif note.get("summary_text"):
        blocks += [Block(kind="paragraph", text=p.strip()) for p in note["summary_text"].split("\n\n") if p.strip()]
    transcript = note.get("transcript") or []
    if transcript:
        blocks.append(Block(kind="heading", text="Transcript", level=2))
        blocks += transcript_blocks(transcript, owner.get("name") or owner.get("email") or "")

    metadata = {
        "meeting_date": event.get("scheduled_start_time") or note.get("created_at"),
        "attendees": attendees,
        "space": ", ".join(s.get("name", "") for s in note.get("space_membership") or []),
        "folder": ", ".join(f.get("name", "") for f in note.get("folder_membership") or []),
        "organiser": event.get("organiser") or "",
        "owner": owner.get("email", ""),
        "granola_id": note.get("id", ""),
        "has_transcript": bool(transcript),
    }
    return Document(title=title, kind="meeting", blocks=blocks, metadata=metadata)


# ---------------------------------------------------------------------------
# Connector


class GranolaConnector(Connector):
    kind = "granola"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.space_ids: list[str] = list(config.get("space_ids") or [])
        self.folder_ids: list[str] = list(config.get("folder_ids") or [])
        self._limiter = RateLimiter(float(config.get("requests_per_second", 4)))
        self._client: httpx.AsyncClient | None = None
        self._folders: dict[str, dict[str, Any]] | None = None
        self._spaces: dict[str, str] | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            key = settings().granola_api_key
            if not key:
                raise ValueError("NEBULA_GRANOLA_API_KEY is not set")
            self._client = httpx.AsyncClient(
                base_url=API, headers={"Authorization": f"Bearer {key}"}, timeout=httpx.Timeout(60.0, connect=15.0)
            )
        return self._client

    async def _send(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return await send_with_retry(self._http(), "GET", path, params=params, limiter=self._limiter)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._send(path, params)
        if response.is_error:
            raise SourceError("Granola", response)
        return response.json()

    async def _paginate(self, path: str, key: str, params: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            query = {**(params or {}), "page_size": PAGE_SIZE, **({"cursor": cursor} if cursor else {})}
            data = await self._get(path, query)
            for item in data.get(key, []):
                yield item
            cursor = data.get("cursor")
            if not data.get("hasMore") or not cursor:
                return

    # -- Listing ----------------------------------------------------------------

    async def list_changes(self, cursor: str | None) -> ChangeBatch:
        if not self.space_ids and not self.folder_ids:
            raise ValueError("Granola collections need space_ids and/or folder_ids; Nebula never imports a whole workspace")
        high_water = parse_time(json.loads(cursor).get("hwm")) if cursor else None
        since = high_water - SAFETY_WINDOW if high_water else None
        self._folders = self._spaces = None  # refresh names once per listing
        out: dict[str, Change] = {}
        newest = high_water
        scopes = [("space_id", s) for s in self.space_ids] + [("folder_id", f) for f in self.folder_ids]
        for param, scope_id in scopes:
            label = await self._scope_name(param, scope_id)
            params = {param: scope_id}
            if since is not None:
                params["updated_after"] = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            async for note in self._paginate("/notes", "notes", params):
                updated = parse_time(note.get("updated_at"))
                if updated and (newest is None or updated > newest):
                    newest = updated
                out.setdefault(note["id"], Change(
                    external_id=note["id"],
                    deleted=bool(note.get("deleted_at")),
                    title=note.get("title") or "Untitled meeting",
                    mime_type=NOTE_MIME,
                    modified_at=updated,
                    path=[label] if label else [],
                ))
        next_cursor = json.dumps({"hwm": newest.isoformat() if newest else None})
        return ChangeBatch(changes=list(out.values()), next_cursor=next_cursor, has_more=False)

    async def _scope_name(self, param: str, scope_id: str) -> str:
        if param == "space_id":
            return (await self._space_map()).get(scope_id, "")
        return ((await self._folder_map()).get(scope_id) or {}).get("name", "")

    async def _space_map(self) -> dict[str, str]:
        if self._spaces is None:
            self._spaces = {s["id"]: s.get("name", "") async for s in self._paginate("/spaces", "spaces")}
        return self._spaces

    async def _folder_map(self) -> dict[str, dict[str, Any]]:
        if self._folders is None:
            self._folders = {f["id"]: f async for f in self._paginate("/folders", "folders")}
        return self._folders

    # -- Fetch -------------------------------------------------------------------

    async def fetch(self, change: Change) -> FetchedItem | None:
        note = await self._get_note(change.external_id)
        if note is None or note.get("deleted_at"):
            return None
        if not await self._allowed(note):
            log.warning("Granola note %s is no longer in an allowed space or folder; skipping", change.external_id)
            return None
        document = note_to_document(note)
        owner = note.get("owner") or {}
        return FetchedItem(
            external_id=note["id"],
            title=document.title,
            url=note.get("web_url", ""),
            mime_type=NOTE_MIME,
            kind="meeting",
            modified_at=parse_time(note.get("updated_at")),
            created_at=parse_time(note.get("created_at")),
            author=owner.get("email") or owner.get("name") or "",
            path=list(change.path),
            document=document,
            extra={"has_transcript": document.metadata["has_transcript"], "audience": note_audience(note)},
        )

    async def _get_note(self, note_id: str) -> dict[str, Any] | None:
        """The note with its transcript, paging the transcript when it's too large to inline."""
        response = await self._send(f"/notes/{note_id}", {"include": "transcript"})
        if response.status_code == 404:
            return None
        if response.status_code == 413:  # TRANSCRIPT_TOO_LARGE
            note = await self._get(f"/notes/{note_id}")
            note["transcript"] = [item async for item in self._transcript_pages(note_id)]
            return note
        if response.is_error:
            raise SourceError("Granola", response)
        return response.json()

    async def _transcript_pages(self, note_id: str) -> AsyncIterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            params = {"page_size": TRANSCRIPT_PAGE_SIZE, **({"cursor": cursor} if cursor else {})}
            data = await self._get(f"/notes/{note_id}/transcript", params)
            for item in data.get("transcript", []):
                yield item
            cursor = data.get("cursor")
            if not data.get("hasMore") or not cursor:
                return

    async def _allowed(self, note: dict[str, Any]) -> bool:
        """Is the note (still) in an allowlisted space, or under an allowlisted folder?"""
        spaces = set(self.space_ids)
        allowed_folders = set(self.folder_ids)
        if any(s.get("id") in spaces for s in note.get("space_membership") or []):
            return True
        known = await self._folder_map() if allowed_folders else {}
        for folder in note.get("folder_membership") or []:
            if folder.get("space_id") in spaces:
                return True
            lookup = {**known, folder.get("id"): folder}
            current: str | None = folder.get("id")
            seen: set[str] = set()
            while current and current not in seen:  # walk up through parent folders
                if current in allowed_folders:
                    return True
                seen.add(current)
                current = (lookup.get(current) or {}).get("parent_folder_id")
        return False

    async def source_audience(self) -> SourceAudience:
        return SourceAudience(
            unknown=True,
            note="Granola's API lists spaces and folders but not their members; check space membership in Granola.",
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
