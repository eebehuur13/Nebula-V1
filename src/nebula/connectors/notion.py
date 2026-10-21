"""Notion connector: a page tree and/or data sources, read with an internal integration.

config: {"root_page_id": "..."} and/or {"data_source_ids": [...]}; with neither,
everything the integration can see. Optional {"exclude_page_ids": [...]} leaves pages and
everything under them out (Notion's API can't say who may read a page, so restricted pages are
left out rather than shown to the whole collection), and {"requests_per_second": 3}.

Notion has no change feed, so listing uses the search endpoint sorted by
last_edited_time (newest first) and stops at the cursor's high-water mark minus
a safety window (Notion rounds last_edited_time to the minute). Items are kept
when walking their parents reaches a root. API version is pinned to 2026-03-11,
where databases hold data sources and rows are pages under a data source.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from nebula.config import settings
from nebula.connectors._http import RETRY_STATUSES, RateLimiter, SourceError, parse_time, send_with_retry
from nebula.connectors.base import Change, ChangeBatch, Connector, FetchedItem, SourceAudience
from nebula.ingest.types import Block, Document

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
SAFETY_WINDOW = timedelta(minutes=2)
PAGE_MIME = "application/vnd.notion.page"
DATA_SOURCE_MIME = "application/vnd.notion.data-source"
MAX_ANCESTRY = 40
MAX_BLOCK_DEPTH = 12
MAX_BLOCKS = 20_000
CACHE_LIMIT = 20_000
LIST_TYPES = {"bulleted_list_item", "numbered_list_item", "to_do"}
PARENT_KINDS = ("page_id", "data_source_id", "database_id", "block_id")


def _nid(notion_id: str) -> str:
    """Normalise a Notion id (with or without dashes) for comparisons."""
    return notion_id.replace("-", "").lower()


def _is_trashed(obj: dict[str, Any]) -> bool:
    return bool(obj.get("in_trash") or obj.get("is_archived") or obj.get("archived"))


def _notion_retryable(response: httpx.Response) -> bool:
    if response.status_code not in RETRY_STATUSES:
        return False
    if response.status_code == 429:
        try:
            reason = (response.json().get("additional_data") or {}).get("rate_limit_reason")
        except ValueError:
            reason = None
        return reason != "public_api_request_blocked"
    return True


# ---------------------------------------------------------------------------
# Rich text, properties and blocks -> text


def rich_text(items: list[dict[str, Any]] | None) -> str:
    """Notion rich text to plain text, keeping links as markdown links."""
    parts = []
    for item in items or []:
        text = item.get("plain_text", "")
        href = item.get("href")
        parts.append(f"[{text}]({href})" if href and text and text != href else text)
    return "".join(parts)


def page_title(page: dict[str, Any]) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return rich_text(prop.get("title"))
    return ""


def object_title(obj: dict[str, Any]) -> str:
    if obj.get("object") == "page":
        return page_title(obj)
    return rich_text(obj.get("title"))


def property_text(prop: dict[str, Any]) -> str:
    """A database property value as short text ('' when empty)."""
    kind = prop.get("type", "")
    value = prop.get(kind)
    if value is None:
        return ""
    if kind in ("title", "rich_text"):
        return rich_text(value)
    if kind in ("select", "status"):
        return value.get("name", "")
    if kind == "multi_select":
        return ", ".join(option.get("name", "") for option in value)
    if kind == "date":
        return " to ".join(x for x in (value.get("start"), value.get("end")) if x)
    if kind in ("people", "created_by", "last_edited_by"):
        people = value if isinstance(value, list) else [value]
        return ", ".join(p.get("name") or (p.get("person") or {}).get("email") or p.get("id", "") for p in people)
    if kind == "files":
        return ", ".join(f.get("name", "") for f in value)
    if kind == "checkbox":
        return "Yes" if value else "No"
    if kind == "relation":
        return f"{len(value)} linked page(s)" if value else ""
    if kind == "formula":
        return _scalar(value.get(value.get("type", ""), ""))
    if kind == "rollup":
        inner = value.get(value.get("type", ""))
        if isinstance(inner, list):
            return ", ".join(t for t in (property_text(x) for x in inner) if t)
        return _scalar(inner)
    if kind == "unique_id":
        return "-".join(str(x) for x in (value.get("prefix"), value.get("number")) if x is not None)
    if kind == "verification":
        return value.get("state", "")
    return _scalar(value)


def _scalar(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        if isinstance(value, dict) and "start" in value:
            return value.get("start") or ""
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    cells = [[c.replace("|", "\\|").replace("\n", " ") for c in row] + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(cells[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(row) + " |" for row in cells[1:]]
    return "\n".join(lines)


def properties_block(page: dict[str, Any]) -> Block | None:
    """The row's properties (minus the title) as a two-column table."""
    rows = [["Property", "Value"]]
    for name, prop in (page.get("properties") or {}).items():
        if prop.get("type") == "title":
            continue
        text = property_text(prop)
        if text:
            rows.append([name, text])
    if len(rows) == 1:
        return None
    return Block(kind="table", text=markdown_table(rows), table=rows, locator={"block_id": page["id"]})


def blocks_to_document(tree: list[dict[str, Any]]) -> list[Block]:
    """Convert a loaded block tree (children under `_children`) to Document blocks."""
    out: list[Block] = []
    _convert_siblings(tree, out)
    return out


def _convert_siblings(blocks: list[dict[str, Any]], out: list[Block]) -> None:
    i = 0
    while i < len(blocks):
        if blocks[i].get("type") in LIST_TYPES:
            j = i
            while j < len(blocks) and blocks[j].get("type") in LIST_TYPES:
                j += 1
            lines: list[str] = []
            _list_lines(blocks[i:j], 0, lines)
            out.append(Block(kind="list", text="\n".join(lines), locator={"block_id": blocks[i]["id"]}))
            i = j
            continue
        _convert_block(blocks[i], out)
        i += 1


def _list_lines(items: list[dict[str, Any]], depth: int, lines: list[str]) -> None:
    indent = "  " * depth
    number = 0
    for item in items:
        kind = item.get("type", "")
        if kind not in LIST_TYPES:  # content nested inside a list item
            text = flat_text(item)
            if text:
                lines.append(indent + text.replace("\n", "\n" + indent))
            continue
        number = number + 1 if kind == "numbered_list_item" else 0
        data = item.get(kind) or {}
        if kind == "numbered_list_item":
            marker = f"{number}."
        elif kind == "to_do":
            marker = "- [x]" if data.get("checked") else "- [ ]"
        else:
            marker = "-"
        lines.append(f"{indent}{marker} {rich_text(data.get('rich_text'))}".rstrip())
        _list_lines(item.get("_children", []), depth + 1, lines)


def flat_text(block: dict[str, Any]) -> str:
    """A block and its children as plain text (used inside list items)."""
    sub: list[Block] = []
    _convert_siblings([block], sub)
    return "\n".join(b.text for b in sub if b.text)


def _table_rows(table: dict[str, Any]) -> list[list[str]]:
    return [
        [rich_text(cell) for cell in (row.get("table_row") or {}).get("cells", [])]
        for row in table.get("_children", [])
        if row.get("type") == "table_row"
    ]


def _media_text(data: dict[str, Any]) -> tuple[str, str]:
    """(caption or name, external url) for file-like blocks. Notion-hosted URLs expire, so we drop them."""
    label = rich_text(data.get("caption")) or data.get("name", "")
    url = (data.get("external") or {}).get("url", "") if data.get("type") == "external" else ""
    return label, url


def _convert_block(block: dict[str, Any], out: list[Block]) -> None:
    kind = block.get("type", "")
    data = block.get(kind) or {}
    loc = {"block_id": block.get("id", "")}
    text = rich_text(data.get("rich_text"))
    children = block.get("_children", [])

    def add(block_kind: str, value: str, **extra: Any) -> None:
        if value:
            out.append(Block(kind=block_kind, text=value, locator=loc, **extra))

    if kind in ("heading_1", "heading_2", "heading_3", "heading_4"):
        add("heading", text, level=int(kind[-1]))
    elif kind in ("paragraph", "toggle", "template"):
        add("paragraph", text)
    elif kind in ("quote", "callout"):
        add("quote", text)
    elif kind == "code":
        add("code", text)
    elif kind == "equation":
        add("paragraph", data.get("expression", ""))
    elif kind == "table":
        rows = _table_rows(block)
        if rows:
            out.append(Block(kind="table", text=markdown_table(rows), table=rows, locator=loc))
        return
    elif kind == "child_page":
        add("paragraph", f"Sub-page: {data.get('title', '')}")
        return
    elif kind == "child_database":
        add("paragraph", f"Database: {data.get('title', '')}")
        return
    elif kind in ("bookmark", "link_preview", "embed"):
        url = data.get("url", "")
        caption = rich_text(data.get("caption"))
        add("paragraph", f"[{caption or url}]({url})" if url else caption)
    elif kind in ("image", "file"):
        label, _ = _media_text(data)
        out.append(Block(kind="image", text=label, locator=loc))
    elif kind in ("pdf", "video", "audio"):
        label, url = _media_text(data)
        add("paragraph", f"[{label or url}]({url})" if url else label)
    elif kind == "meeting_notes":
        add("heading", rich_text(data.get("title")) or "Meeting notes", level=2)
        for label, section in block.get("_sections", []):
            out.append(Block(kind="heading", text=label, level=3, locator=loc))
            _convert_siblings(section, out)
    # divider, table_of_contents, breadcrumb, link_to_page, unsupported: nothing to add.
    # column_list, column, synced_block, tab and unknown containers: just their children.
    _convert_siblings(children, out)


# ---------------------------------------------------------------------------
# Connector


@dataclass
class _Node:
    """An ancestor in the parent walk: its title ('' for blocks) and its own parent."""

    id: str
    title: str
    parent: dict[str, Any]


class NotionConnector(Connector):
    kind = "notion"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.root_page_id = _nid(config.get("root_page_id") or "")
        self.data_source_ids = [_nid(x) for x in config.get("data_source_ids") or []]
        self._roots = {x for x in [self.root_page_id, *self.data_source_ids] if x}
        self._excluded = {_nid(x) for x in config.get("exclude_page_ids") or []}
        self._limiter = RateLimiter(float(config.get("requests_per_second", 3)))
        self._client: httpx.AsyncClient | None = None
        self._nodes: dict[str, _Node | None] = {}
        self._users: dict[str, str] = {}

    # -- HTTP -----------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            token = settings().notion_token
            if not token:
                raise ValueError("NEBULA_NOTION_TOKEN is not set")
            self._client = httpx.AsyncClient(
                base_url=API,
                headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
                timeout=httpx.Timeout(60.0, connect=15.0),
            )
        return self._client

    async def _call(self, method: str, path: str, *, body: dict[str, Any] | None = None,
                    params: dict[str, Any] | None = None, missing_ok: bool = False) -> dict[str, Any] | None:
        response = await send_with_retry(
            self._http(), method, path, json=body, params=params,
            limiter=self._limiter, retry_if=_notion_retryable,
        )
        if missing_ok and response.status_code in (403, 404):
            return None
        if response.is_error:
            raise SourceError("Notion", response)
        return response.json()

    async def _paginate(self, method: str, path: str, body: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            page = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            if method == "GET":
                data = await self._call("GET", path, params=page)
            else:
                data = await self._call("POST", path, body={**(body or {}), **page})
            data = data or {}
            for result in data.get("results", []):
                yield result
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return

    # -- Listing ----------------------------------------------------------------

    async def list_changes(self, cursor: str | None) -> ChangeBatch:
        high_water = parse_time(json.loads(cursor).get("hwm")) if cursor else None
        since = high_water - SAFETY_WINDOW if high_water else None
        self._nodes.clear()  # titles and moves since last time
        out: dict[str, Change] = {}
        newest = high_water

        if self.root_page_id or not self.data_source_ids:
            async for obj in self._search(since):
                newest = _later(newest, obj)
                if change := await self._change_for(obj, incremental=since is not None):
                    out[change.external_id] = change
        for ds_id in self.data_source_ids:
            async for obj in self._data_source_items(ds_id, since):
                newest = _later(newest, obj)
                if change := await self._change_for(obj, incremental=since is not None):
                    out[change.external_id] = change
        if since is not None:
            # Trashed items drop out of normal search; ask for them explicitly. Emitting a
            # deletion for something outside the collection is a no-op for the worker.
            async for obj in self._search(since, in_trash=True):
                newest = _later(newest, obj)
                out[obj["id"]] = Change(external_id=obj["id"], deleted=True, title=object_title(obj))

        next_cursor = json.dumps({"hwm": newest.isoformat() if newest else None})
        return ChangeBatch(changes=list(out.values()), next_cursor=next_cursor, has_more=False)

    async def _search(self, since: datetime | None, in_trash: bool = False) -> AsyncIterator[dict[str, Any]]:
        """Pages and data sources, newest edit first, stopping before `since`."""
        body: dict[str, Any] = {"sort": {"timestamp": "last_edited_time", "direction": "descending"}}
        if in_trash:
            body["filter"] = {"in_trash": True}
        async with aclosing(self._paginate("POST", "/search", body)) as results:
            async for obj in results:
                edited = parse_time(obj.get("last_edited_time"))
                if since is not None and edited is not None and edited < since:
                    return
                yield obj

    async def _data_source_items(self, ds_id: str, since: datetime | None) -> AsyncIterator[dict[str, Any]]:
        """A configured data source itself, then its rows edited since `since`."""
        source = await self._call("GET", f"/data_sources/{ds_id}", missing_ok=True)
        if source is None:
            log.warning("Notion data source %s is not shared with the integration", ds_id)
            return
        edited = parse_time(source.get("last_edited_time"))
        if since is None or edited is None or edited >= since:
            yield source
        body: dict[str, Any] = {"sorts": [{"timestamp": "last_edited_time", "direction": "descending"}]}
        if since is not None:
            body["filter"] = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since.isoformat()}}
        async for row in self._paginate("POST", f"/data_sources/{ds_id}/query", body):
            yield row

    async def _change_for(self, obj: dict[str, Any], incremental: bool) -> Change | None:
        if obj.get("object") not in ("page", "data_source"):
            return None
        path = await self._locate(obj)
        if path is None:
            # Outside the collection. In an incremental pass it may have just been moved out,
            # so delete it (a no-op if it was never indexed); a full listing just leaves it out.
            return Change(external_id=obj["id"], deleted=True, title=object_title(obj)) if incremental else None
        parent = obj.get("parent") or {}
        parent_id = parent.get(parent.get("type", ""))
        return Change(
            external_id=obj["id"],
            deleted=_is_trashed(obj),
            title=object_title(obj),
            url=obj.get("url", ""),
            mime_type=DATA_SOURCE_MIME if obj["object"] == "data_source" else PAGE_MIME,
            modified_at=parse_time(obj.get("last_edited_time")),
            path=path,
            parent_external_id=parent_id if isinstance(parent_id, str) else None,
        )

    async def _locate(self, obj: dict[str, Any]) -> list[str] | None:
        """Ancestor titles (root first) if `obj` is inside the collection, else None."""
        if _nid(obj["id"]) in self._excluded:
            return None
        if _nid(obj["id"]) in self._roots:
            return []
        titles: list[str] = []
        ref = _parent_ref(obj)
        for _ in range(MAX_ANCESTRY):
            node = await self._node(ref)
            if node is None:
                break
            if node.id in self._excluded:
                return None
            if node.title:
                titles.append(node.title)
            if node.id in self._roots:
                return titles[::-1]
            ref = node.parent
        return None if self._roots else titles[::-1]

    async def _node(self, ref: dict[str, Any]) -> _Node | None:
        """One ancestor, cached. None at the workspace top or where access stops."""
        kind = ref.get("type", "")
        if kind not in PARENT_KINDS or not ref.get(kind):
            return None
        key = _nid(ref[kind])
        if key in self._nodes:
            return self._nodes[key]
        endpoint = {"page_id": "pages", "data_source_id": "data_sources", "database_id": "databases", "block_id": "blocks"}[kind]
        obj = await self._call("GET", f"/{endpoint}/{ref[kind]}", missing_ok=True)
        node = None
        if obj is not None:
            title = "" if kind == "block_id" else object_title(obj)
            node = _Node(id=key, title=title, parent=_parent_ref(obj))
        if len(self._nodes) >= CACHE_LIMIT:
            self._nodes.clear()
        self._nodes[key] = node
        return node

    # -- Fetch -------------------------------------------------------------------

    async def fetch(self, change: Change) -> FetchedItem | None:
        if change.mime_type == DATA_SOURCE_MIME:
            return await self._fetch_data_source(change)
        page = await self._call("GET", f"/pages/{change.external_id}", missing_ok=True)
        if page is None or _is_trashed(page):
            return None
        blocks: list[Block] = []
        is_row = (page.get("parent") or {}).get("type") in ("data_source_id", "database_id")
        if is_row and (props := properties_block(page)):
            blocks.append(props)
        tree = await self._load_children(page["id"], depth=0, budget=[MAX_BLOCKS])
        blocks += blocks_to_document(tree)
        title = page_title(page) or change.title or "Untitled"
        author = await self._user_name((page.get("created_by") or {}).get("id", ""))
        document = Document(title=title, kind="notion_page", blocks=blocks, metadata={
            "notion_id": page["id"], "url": page.get("url", ""), "created_by": author,
            "parent_type": (page.get("parent") or {}).get("type", ""),
        })
        return FetchedItem(
            external_id=change.external_id, title=title, url=page.get("url", ""), mime_type=PAGE_MIME,
            kind="notion_page", modified_at=parse_time(page.get("last_edited_time")),
            created_at=parse_time(page.get("created_time")), author=author, path=list(change.path),
            document=document,
        )

    async def _fetch_data_source(self, change: Change) -> FetchedItem | None:
        """A data source becomes a short card: description and property schema. Rows are pages."""
        source = await self._call("GET", f"/data_sources/{change.external_id}", missing_ok=True)
        if source is None or _is_trashed(source):
            return None
        title = rich_text(source.get("title")) or change.title or "Untitled database"
        blocks: list[Block] = []
        if description := rich_text(source.get("description")):
            blocks.append(Block(kind="paragraph", text=description, locator={"block_id": source["id"]}))
        rows = [["Property", "Type", "Options"]]
        for name, prop in (source.get("properties") or {}).items():
            kind = prop.get("type", "")
            options = (prop.get(kind) or {}).get("options") or []
            rows.append([name, kind, ", ".join(o.get("name", "") for o in options)])
        blocks.append(Block(kind="table", text=markdown_table(rows), table=rows, locator={"block_id": source["id"]}))
        author = await self._user_name((source.get("created_by") or {}).get("id", ""))
        return FetchedItem(
            external_id=change.external_id, title=title, url=source.get("url", ""), mime_type=DATA_SOURCE_MIME,
            kind="notion_db", modified_at=parse_time(source.get("last_edited_time")),
            created_at=parse_time(source.get("created_time")), author=author, path=list(change.path),
            document=Document(title=title, kind="notion_db", blocks=blocks,
                              metadata={"notion_id": source["id"], "url": source.get("url", "")}),
        )

    async def _load_children(self, block_id: str, depth: int, budget: list[int]) -> list[dict[str, Any]]:
        """Children of a block, recursively, under `_children` (bounded by depth and a block budget)."""
        children: list[dict[str, Any]] = []
        async for block in self._paginate("GET", f"/blocks/{block_id}/children"):
            budget[0] -= 1
            if budget[0] < 0:
                log.warning("Notion page %s exceeds %d blocks; truncated", block_id, MAX_BLOCKS)
                break
            kind = block.get("type")
            if kind == "meeting_notes":
                block["_sections"] = await self._meeting_sections(block, depth, budget)
            elif block.get("has_children") and kind not in ("child_page", "child_database") and depth < MAX_BLOCK_DEPTH:
                block["_children"] = await self._load_children(block["id"], depth + 1, budget)
            children.append(block)
        return children

    async def _meeting_sections(
        self, block: dict[str, Any], depth: int, budget: list[int]
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        """Notion AI meeting notes keep summary/notes/transcript under pointer blocks."""
        pointers = (block.get("meeting_notes") or {}).get("children") or {}
        sections = []
        for key, label in (("summary_block_id", "Summary"), ("notes_block_id", "Notes"), ("transcript_block_id", "Transcript")):
            if pointers.get(key) and depth < MAX_BLOCK_DEPTH:
                sections.append((label, await self._load_children(pointers[key], depth + 1, budget)))
        return sections

    async def _user_name(self, user_id: str) -> str:
        if not user_id:
            return ""
        if user_id not in self._users:
            user = await self._call("GET", f"/users/{user_id}", missing_ok=True) or {}
            self._users[user_id] = (user.get("person") or {}).get("email") or user.get("name") or ""
        return self._users[user_id]

    async def source_audience(self) -> SourceAudience:
        return SourceAudience(
            unknown=True,
            note="Notion's API doesn't expose who can open a page; check the root page's sharing in Notion.",
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _parent_ref(obj: dict[str, Any]) -> dict[str, Any]:
    """Where to continue the parent walk. Data sources skip their database container."""
    if obj.get("object") == "data_source" and obj.get("database_parent"):
        return obj["database_parent"]
    return obj.get("parent") or {}


def _later(current: datetime | None, obj: dict[str, Any]) -> datetime | None:
    edited = parse_time(obj.get("last_edited_time"))
    if edited is None:
        return current
    return edited if current is None or edited > current else current
