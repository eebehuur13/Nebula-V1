"""Source connectors and the registry the sync worker uses to build them."""

from __future__ import annotations

from typing import Any

from nebula.connectors.base import Change, ChangeBatch, Connector, FetchedItem, SourceAudience
from nebula.connectors.gdrive import GoogleDriveConnector
from nebula.connectors.granola import GranolaConnector
from nebula.connectors.notion import NotionConnector

CONNECTORS: dict[str, type[Connector]] = {
    GoogleDriveConnector.kind: GoogleDriveConnector,
    NotionConnector.kind: NotionConnector,
    GranolaConnector.kind: GranolaConnector,
}


def get_connector(kind: str, config: dict[str, Any]) -> Connector:
    """A connector for one collection. Raises ValueError for an unknown kind or bad config."""
    try:
        cls = CONNECTORS[kind]
    except KeyError:
        raise ValueError(f"unknown connector kind {kind!r}; expected one of {sorted(CONNECTORS)}") from None
    return cls(config or {})


__all__ = [
    "CONNECTORS",
    "Change",
    "ChangeBatch",
    "Connector",
    "FetchedItem",
    "GoogleDriveConnector",
    "GranolaConnector",
    "NotionConnector",
    "SourceAudience",
    "get_connector",
]
