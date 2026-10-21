"""The structured form every source is turned into.

Parsers (files) and connectors (Notion blocks, Granola notes) both produce a
`Document`. The chunker turns a Document into `Chunk`s for the search index, and
`open` serves the Document back to Claude with its structure intact.

Keep this module dependency-free: it is shared by parsers, connectors, the
indexer and the MCP tools.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

BlockKind = Literal[
    "heading",       # level 1-6, text is the heading text
    "paragraph",
    "list",          # text is markdown list ("- a\n- b")
    "table",         # table holds rows (first row = header); text is a markdown rendering
    "code",
    "quote",
    "slide",         # one slide: text is title + body + speaker notes, locator {"slide": n}
    "image",         # text is alt text / caption if any
    "speaker_turn",  # meeting transcript turn: text "Name: words", locator {"t": "00:12:30"}
]

DocKind = Literal["doc", "pdf", "slides", "sheet", "notion_page", "notion_db", "meeting", "text", "html", "other"]

SheetKind = Literal[
    "data",   # a table of records -> loaded as a real SQL table, indexed via its card
    "text",   # mostly text cells (trackers, plans, FAQs) -> each row becomes a chunk
    "other",  # dashboards, pivots, formula-heavy layouts -> card only
]


@dataclass
class Block:
    kind: BlockKind
    text: str
    level: int | None = None
    # Where this block lives in the source, for citations and `open`:
    # {"page": 3} / {"slide": 5} / {"sheet": "Q2"} / {"t": "00:12:30"} / {"block_id": "..."}
    locator: dict[str, Any] = field(default_factory=dict)
    table: list[list[str]] | None = None


@dataclass
class Sheet:
    name: str
    kind: SheetKind
    header: list[str]
    rows: list[list[Any]]          # all data rows (excluding header), values as Python scalars
    n_rows: int
    n_cols: int
    has_formulas: bool = False
    header_row_index: int = 0      # 0-based row index of the header within the sheet
    notes: str = ""                # anything the classifier noticed (merged cells, multiple tables...)


@dataclass
class Document:
    title: str
    kind: DocKind
    blocks: list[Block] = field(default_factory=list)
    sheets: list[Sheet] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Document":
        return Document(
            title=d.get("title", ""),
            kind=d.get("kind", "other"),
            blocks=[Block(**b) for b in d.get("blocks", [])],
            sheets=[Sheet(**s) for s in d.get("sheets", [])],
            metadata=d.get("metadata", {}),
        )


ChunkKind = Literal["text", "slide", "row", "card", "turn"]


@dataclass
class Chunk:
    text: str                                  # the text that is embedded and keyword-indexed
    heading_path: list[str] = field(default_factory=list)  # e.g. ["Q2 report", "Andhra Pradesh"]
    locator: dict[str, Any] = field(default_factory=dict)  # merged locator of the blocks it covers
    kind: ChunkKind = "text"
