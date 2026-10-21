"""Turn a `Document` into search `Chunk`s.

Every chunk's text starts with a breadcrumb line ("Title › Heading › Subheading") so it
makes sense on its own in search results, and no chunk is longer than `max_chars`.

- Prose: blocks gather under the current heading path; a heading of level 1-2 or a full
  chunk starts a new one. Tables split between rows with the header repeated. Only
  paragraphs too long for one chunk are split mid-way, on sentence boundaries (". ", "?",
  "!", Hindi "।"), with `overlap_chars` of the previous piece repeated.
- Slides: one chunk per slide; consecutive tiny slides (< 200 chars) are merged.
- Meeting turns: consecutive speaker turns in windows, located by t_start/t_end.
- Sheets: "data"/"other" sheets give one card chunk; "text" sheets give row chunks written
  as "column: value" lines.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from nebula.ingest.parse import markdown_table_lines
from nebula.ingest.sheets import CARD_MAX_CHARS, SEP, format_value, is_blank, sheet_card
from nebula.ingest.types import Block, Chunk, ChunkKind, Document, Sheet

__all__ = ["chunk_document"]

TINY_SLIDE_CHARS = 200
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?।॥])[\"'”’)\]]*\s+|\n+")


def chunk_document(doc: Document, max_chars: int = 1800, overlap_chars: int = 200) -> list[Chunk]:
    """Chunks for the search index, in document order (blocks first, then sheets)."""
    return _Chunker(doc, max(200, max_chars), max(0, overlap_chars)).run()


# --------------------------------------------------------------------------- text splitting

def _hard_split(text: str, size: int) -> list[str]:
    """Split text with no usable sentence breaks at whitespace (or anywhere, if there is none)."""
    pieces = []
    while len(text) > size:
        cut = text.rfind(" ", size // 2, size)
        cut = cut if cut > 0 else size
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        pieces.append(text)
    return pieces


def _tail(text: str, overlap: int) -> str:
    """The last ~`overlap` chars of a piece, starting at a sentence or word boundary."""
    if overlap <= 0 or len(text) <= overlap:
        return text if overlap > 0 else ""
    tail = text[-overlap:]
    sentences = _SENTENCE_SPLIT.split(tail)
    if len(sentences) > 1 and sentences[-1].strip() and len(sentences[-1]) >= overlap // 3:
        return sentences[-1].strip()
    space = tail.find(" ")
    return "…" + (tail[space + 1:] if 0 <= space < len(tail) - 1 else tail)


def split_prose(text: str, size: int, overlap: int) -> list[str]:
    """Split a long paragraph into pieces of at most `size` chars on sentence boundaries;
    each piece after the first starts with the tail of the previous one."""
    if len(text) <= size:
        return [text]
    overlap = min(overlap, size // 3)
    units: list[str] = []
    for s in _SENTENCE_SPLIT.split(text):
        s = s.strip()
        if s:
            units.extend(_hard_split(s, size - overlap - 2) if len(s) > size - overlap - 2 else [s])
    pieces: list[str] = []
    cur = ""
    for u in units:
        if cur and len(cur) + 1 + len(u) > size:
            pieces.append(cur)
            lead = _tail(cur, overlap)
            cur = f"{lead} {u}" if lead and len(lead) + 1 + len(u) <= size else u
        else:
            cur = f"{cur} {u}" if cur else u
    if cur:
        pieces.append(cur)
    return pieces


def _pack(lines: Iterable[str], size: int, overlap: int = 0, sep: str = "\n") -> list[str]:
    """Greedily pack lines into pieces of at most `size` chars; over-long lines are split as prose."""
    pieces: list[str] = []
    cur = ""
    for line in lines:
        for part in split_prose(line, size, overlap) if len(line) > size else [line]:
            if cur and len(cur) + len(sep) + len(part) > size:
                pieces.append(cur)
                cur = part
            else:
                cur = f"{cur}{sep}{part}" if cur else part
    if cur:
        pieces.append(cur)
    return pieces


def _merge_locators(locs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """{"page": 3} + {"page": 4} -> {"pages": [3, 4]}; a single value stays singular."""
    values: dict[str, list[Any]] = {}
    for loc in locs:
        for k, v in loc.items():
            bucket = values.setdefault(k, [])
            for item in v if isinstance(v, list) else [v]:
                if item not in bucket:
                    bucket.append(item)
    out: dict[str, Any] = {}
    for k, vs in values.items():
        if len(vs) == 1 and not k.endswith("s"):
            out[k] = vs[0]
        else:
            if all(isinstance(v, int | float) for v in vs):
                vs = sorted(vs)
            out[k if k.endswith("s") else k + "s"] = vs
    return out


# --------------------------------------------------------------------------- the chunker

class _Chunker:
    def __init__(self, doc: Document, max_chars: int, overlap: int) -> None:
        self.doc = doc
        self.title = " ".join((doc.title or "").split()) or "Untitled"
        self.max_chars = max_chars
        self.overlap = overlap
        self.out: list[Chunk] = []
        self.stack: list[tuple[int, str]] = []  # open headings (level, text)
        # The prose chunk being built.
        self.parts: list[str] = []
        self.locs: list[dict[str, Any]] = []
        self.path: list[str] = []
        self.crumb = ""
        self.budget = 0

    # -- helpers

    def _current_path(self) -> list[str]:
        path = [self.title]
        for i, (_, h) in enumerate(self.stack):
            if i == 0 and h.casefold() == self.title.casefold():
                continue
            path.append(h)
        return path

    def _breadcrumb(self, path: list[str]) -> str:
        limit = max(40, self.max_chars // 4)
        crumb = SEP.join(p if len(p) <= 80 else p[:79] + "…" for p in path)
        return crumb if len(crumb) <= limit else crumb[: limit - 1] + "…"

    def _emit(self, path: list[str], body: str, locator: dict[str, Any], kind: ChunkKind,
              heading_path: list[str] | None = None) -> None:
        crumb = self._breadcrumb(path)
        text = f"{crumb}\n{body}"
        if len(text) > self.max_chars:  # defensive: callers size bodies to fit
            text = text[: self.max_chars - 1] + "…"
        hp = list(heading_path if heading_path is not None else path)
        self.out.append(Chunk(text=text, heading_path=hp, locator=dict(locator), kind=kind))

    def _body_budget(self, path: list[str]) -> int:
        return self.max_chars - len(self._breadcrumb(path)) - 1

    # -- driver

    def run(self) -> list[Chunk]:
        blocks = self.doc.blocks
        i = 0
        while i < len(blocks):
            b = blocks[i]
            if b.kind in ("slide", "speaker_turn"):
                j = i
                while j < len(blocks) and blocks[j].kind == b.kind:
                    j += 1
                self._flush()
                (self._slides if b.kind == "slide" else self._turns)(blocks[i:j])
                i = j
                continue
            self._prose(b)
            i += 1
        self._flush()
        if not self.out and any(b.kind == "heading" for b in blocks):
            # An outline-only document: index its headings rather than nothing.
            outline = [("  " * ((b.level or 1) - 1)) + b.text for b in blocks if b.kind == "heading"]
            path = [self.title]
            for piece in _pack(outline, self._body_budget(path)):
                self._emit(path, piece, {}, "text")
        for sheet in self.doc.sheets:
            self._sheet(sheet)
        return self.out

    # -- prose

    def _start(self) -> None:
        self.path = self._current_path()
        self.crumb = self._breadcrumb(self.path)
        self.budget = self.max_chars - len(self.crumb) - 1

    def _flush(self) -> None:
        if self.parts:
            self._emit(self.path, "\n\n".join(self.parts), _merge_locators(self.locs), "text")
        self.parts, self.locs = [], []

    def _used(self) -> int:
        return sum(len(p) for p in self.parts) + 2 * max(0, len(self.parts) - 1)

    def _prose(self, b: Block) -> None:
        if b.kind == "heading":
            level = b.level or 1
            text = " ".join(b.text.split())
            if not text:
                return
            if level <= 2:
                self._flush()
            while self.stack and self.stack[-1][0] >= level:
                self.stack.pop()
            self.stack.append((level, text))
            if self.parts:  # a minor heading inside the running chunk stays visible in its text
                if self._used() + 2 + len(text) > self.budget:
                    self._flush()  # the next chunk carries it in its breadcrumb instead
                else:
                    self.parts.append(text)
            return
        text = self._render(b)
        if not text.strip():
            return
        if not self.parts:
            self._start()
        if len(text) > self.budget:
            pieces = self._split_block(b, text, self.budget)
            for piece in pieces[:-1]:
                self._add(piece, b.locator)
                self._flush()
                self._start()
            text = pieces[-1]
        self._add(text, b.locator)

    def _add(self, text: str, locator: dict[str, Any]) -> None:
        if self.parts and self._used() + 2 + len(text) > self.budget:
            self._flush()
        if not self.parts:
            self._start()
        self.parts.append(text)
        if locator:
            self.locs.append(locator)

    @staticmethod
    def _render(b: Block) -> str:
        if b.kind == "table" and b.table:
            return "\n".join(markdown_table_lines(b.table))
        if b.kind == "code":
            return f"```\n{b.text.strip(chr(10))}\n```"
        if b.kind == "quote":
            return "\n".join("> " + ln for ln in b.text.splitlines())
        if b.kind == "image":
            return f"[Image: {b.text.strip()}]" if b.text.strip() else ""
        return b.text.strip()

    def _split_block(self, b: Block, text: str, size: int) -> list[str]:
        """Pieces (each <= size) of a block too big for one chunk."""
        if b.kind == "table" and b.table:
            return self._split_table(b.table, size)
        if b.kind == "code":
            inner = _pack(b.text.strip("\n").splitlines(), size - 8)
            return [f"```\n{p}\n```" for p in inner]
        if b.kind == "list":
            return _pack(text.splitlines(), size, self.overlap)
        if b.kind == "quote":
            return ["> " + p for p in split_prose(b.text, size - 2, self.overlap)]
        return split_prose(text, size, self.overlap)

    def _split_table(self, rows: list[list[str]], size: int) -> list[str]:
        """Split between rows, repeating the header line; a row too big even alone is written
        out as "column: value" lines and split as prose."""
        lines = markdown_table_lines(rows)
        head = "\n".join(lines[:2])
        header = rows[0]
        pieces: list[str] = []
        cur: list[str] = []
        for row, line in zip(rows[1:], lines[2:], strict=False):
            if len(head) + 1 + len(line) > size:
                if cur:
                    pieces.append("\n".join([head, *cur]))
                    cur = []
                pairs = enumerate(zip(header, row, strict=False))
                fields = [f"{h or f'Column {k + 1}'}: {v}" for k, (h, v) in pairs if v]
                pieces.extend(_pack(fields, size, self.overlap))
                continue
            if cur and len(head) + sum(len(c) + 1 for c in cur) + 1 + len(line) > size:
                pieces.append("\n".join([head, *cur]))
                cur = []
            cur.append(line)
        if cur or not pieces:
            pieces.append("\n".join([head, *cur]))
        return pieces

    # -- slides and meeting turns

    def _slides(self, slides: list[Block]) -> None:
        path = self._current_path()
        budget = self._body_budget(path)
        group: list[Block] = []

        def emit_group() -> None:
            if group:
                body = "\n\n".join(s.text.strip() for s in group)
                title = _slide_title(group[0]) if len(group) == 1 else ""
                self._emit(path, body, _merge_locators(s.locator for s in group), "slide",
                           path + [title] if title else path)
                group.clear()

        for s in slides:
            text = s.text.strip()
            if not text:
                continue
            if len(text) < TINY_SLIDE_CHARS:
                if group and sum(len(g.text.strip()) + 2 for g in group) + len(text) > budget:
                    emit_group()
                group.append(s)
                continue
            emit_group()
            title = _slide_title(s)
            hp = path + [title] if title else path  # the slide's own first line names it
            if len(text) <= budget:
                self._emit(path, text, s.locator, "slide", hp)
                continue
            first, _, rest = text.partition("\n")
            cont = f"{first} (cont.)"
            pieces = _pack(rest.splitlines(), budget - len(cont) - 1, self.overlap)
            for k, piece in enumerate(pieces):
                self._emit(path, f"{first if k == 0 else cont}\n{piece}", s.locator, "slide", hp)
        emit_group()

    def _turns(self, turns: list[Block]) -> None:
        path = self._current_path()
        budget = self._body_budget(path)
        group: list[str] = []
        times: list[Any] = []

        def emit() -> None:
            if group:
                loc = {}
                if times:
                    loc = {"t_start": times[0], "t_end": times[-1]}
                self._emit(path, "\n".join(group), loc, "turn")
            group.clear()
            times.clear()

        for b in turns:
            text = b.text.strip()
            if not text:
                continue
            t = b.locator.get("t")
            pieces = [text]
            if len(text) > budget:
                speaker, sep, words = text.partition(": ")
                if sep and len(speaker) <= 60:
                    prefix = f"{speaker}: "
                    pieces = [prefix + p for p in split_prose(words, budget - len(prefix), self.overlap)]
                else:
                    pieces = split_prose(text, budget, self.overlap)
            for piece in pieces:
                if group and sum(len(g) + 1 for g in group) + len(piece) > budget:
                    emit()
                group.append(piece)
                if t is not None:
                    times.append(t)
        emit()

    # -- sheets

    def _sheet(self, sheet: Sheet) -> None:
        path = [self.title, sheet.name]
        if sheet.kind != "text":
            card = sheet_card(sheet, self.title, max_chars=min(CARD_MAX_CHARS, self.max_chars))
            self.out.append(Chunk(text=card, heading_path=path, locator={"sheet": sheet.name}, kind="card"))
            return
        budget = self._body_budget(path)
        n_cols = max(sheet.n_cols, len(sheet.header))
        names = [
            format_value(sheet.header[j], 80) if j < len(sheet.header) and not is_blank(sheet.header[j])
            else f"Column {j + 1}"
            for j in range(n_cols)
        ]
        group: list[str] = []
        first = last = 0

        def emit(rows_from: int, rows_to: int, body: str) -> None:
            self._emit(path, body, {"sheet": sheet.name, "rows": f"{rows_from}-{rows_to}"}, "row")

        for i, row in enumerate(sheet.rows):
            lines = [f"{names[j]}: {format_value(v)}" for j, v in enumerate(row[:n_cols]) if not is_blank(v)]
            if not lines:
                continue
            rownum = sheet.header_row_index + 2 + i  # 1-based spreadsheet row
            text = "\n".join(lines)
            if len(text) > budget:
                if group:
                    emit(first, last, "\n\n".join(group))
                    group = []
                lead = lines[0] if len(lines[0]) <= 100 else ""
                pieces = _pack(lines[1:] if lead else lines, budget - len(lead) - 1, self.overlap)
                for piece in pieces:
                    emit(rownum, rownum, f"{lead}\n{piece}" if lead else piece)
                continue
            if group and sum(len(g) + 2 for g in group) + len(text) > budget:
                emit(first, last, "\n\n".join(group))
                group = []
            if not group:
                first = rownum
            group.append(text)
            last = rownum
        if group:
            emit(first, last, "\n\n".join(group))


def _slide_title(b: Block) -> str:
    first = b.text.strip().split("\n", 1)[0]
    _, sep, title = first.partition(": ")
    return title.strip() if sep and first.startswith("Slide ") else ""
