"""What every source connector provides.

A connector reads one registered collection root (a Shared Drive or folder, a
Notion page tree, a Granola space) with an organisation-owned credential. The
sync worker calls `list_changes` with the last cursor, then `fetch` for each
changed item, parses it into a Document and indexes it.

Connectors never decide who may see something; collections carry their audience
in Nebula. Connectors do report the source's own sharing (`source_audience`) so
the nightly check can flag a collection whose Nebula audience is wider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nebula.ingest.types import Document


@dataclass
class Change:
    external_id: str
    deleted: bool = False
    title: str = ""
    url: str = ""
    mime_type: str = ""
    modified_at: datetime | None = None
    path: list[str] = field(default_factory=list)  # folder / parent titles, root first
    parent_external_id: str | None = None
    # Set when the item is shared more narrowly than the collection root
    # (for example a limited-access folder in a Shared Drive). Skipped unless access mirrors the source.
    narrower_than_root: bool = False
    # The root (a shared folder, file or Shared Drive) the item came in through, for multi-root sources.
    root: str | None = None
    # The source's own revision marker. When it matches what was stored, the content hasn't changed
    # and the worker doesn't download it again. Empty means "unknown": always fetch.
    rev: str = ""
    # A fingerprint of who the source shares the item with. None means the source didn't say.
    acl: str | None = None
    # Sharing above the item changed, so its permissions are re-read even if `acl` looks the same.
    recheck_acl: bool = False


@dataclass
class ChangeBatch:
    changes: list[Change]
    next_cursor: str | None
    has_more: bool = False
    lost_roots: list[str] = field(default_factory=list)  # roots the source no longer lets us read


@dataclass
class FetchedItem:
    external_id: str
    title: str
    url: str
    mime_type: str
    kind: str                      # doc, sheet, slides, pdf, notion_page, notion_db, meeting, file
    modified_at: datetime | None = None
    created_at: datetime | None = None
    author: str = ""
    path: list[str] = field(default_factory=list)
    # Either raw bytes (the worker parses them by filename/mime) ...
    data: bytes | None = None
    filename: str = ""
    # ... or a Document the connector already structured (Notion blocks, Granola notes).
    document: Document | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceAudience:
    """Who can open the root in the source system, as far as the API tells us."""
    emails: set[str] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)    # e.g. Google Group addresses
    domain_wide: bool = False                         # anyone in the Workspace domain
    public: bool = False
    unknown: bool = False                             # API can't tell us
    note: str = ""


class Connector(ABC):
    """One instance per collection. `config` is collections.source_config."""

    kind: str = ""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    @abstractmethod
    async def list_changes(self, cursor: str | None) -> ChangeBatch:
        """Changes under the root since `cursor`. cursor=None means a full listing."""

    @abstractmethod
    async def fetch(self, change: Change) -> FetchedItem | None:
        """Download one item. Return None if it disappeared or can't be read."""

    async def source_audience(self) -> SourceAudience:
        return SourceAudience(unknown=True, note="not implemented for this connector")

    async def close(self) -> None:  # pragma: no cover - optional
        return None
