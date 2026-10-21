"""Turning stored material back into text for Claude, with structure and positions intact."""

from __future__ import annotations

from nebula.ingest.types import Block, Document, Sheet


def _block_md(b: Block) -> str:
    if b.kind == "heading":
        return "#" * min(max(b.level or 1, 1), 6) + " " + b.text
    if b.kind == "slide":
        return f"--- slide {b.locator.get('slide', '')} ---\n{b.text}"
    if b.kind == "code":
        return f"```\n{b.text}\n```"
    if b.kind == "quote":
        return "\n".join("> " + line for line in b.text.splitlines())
    if b.kind == "speaker_turn" and b.locator.get("t"):
        return f"[{b.locator['t']}] {b.text}"
    if b.kind == "image":
        return f"[image: {b.text}]" if b.text else "[image]"
    return b.text


def _sheet_md(s: Sheet, max_rows: int = 30) -> str:
    head = f"### Sheet '{s.name}' ({s.kind}, {s.n_rows} rows × {s.n_cols} columns)"
    if not s.header:
        return head
    rows = [s.header] + [[("" if v is None else str(v)) for v in r] for r in s.rows[:max_rows]]
    width = len(s.header)
    lines = ["| " + " | ".join(str(c).replace("|", "/") for c in (r + [""] * width)[:width]) + " |" for r in rows]
    lines.insert(1, "|" + "---|" * width)
    more = f"\n… {s.n_rows - max_rows} more rows" if s.n_rows > max_rows else ""
    return head + "\n" + "\n".join(lines) + more


def block_page(b: Block) -> int | None:
    p = b.locator.get("page")
    return int(p) if isinstance(p, (int, str)) and str(p).isdigit() else None


def select(doc: Document, part: str | None) -> tuple[list[Block], list[Sheet], str]:
    """Narrow a document to one part: 'page:3', 'pages:3-5', 'slide:5', 'sheet:Name', 'section:Heading text'."""
    if not part:
        return doc.blocks, doc.sheets, ""
    kind, _, val = part.partition(":")
    kind, val = kind.strip().lower(), val.strip()
    if kind in ("page", "pages"):
        lo, _, hi = val.partition("-")
        lo_i, hi_i = int(lo), int(hi or lo)
        return [b for b in doc.blocks if (p := block_page(b)) is not None and lo_i <= p <= hi_i], [], f"pages {lo_i}–{hi_i}"
    if kind == "slide":
        return [b for b in doc.blocks if str(b.locator.get("slide")) == val], [], f"slide {val}"
    if kind == "sheet":
        return [], [s for s in doc.sheets if s.name.lower() == val.lower()], f"sheet '{val}'"
    if kind == "section":
        out, inside, level = [], False, 0
        for b in doc.blocks:
            if b.kind == "heading":
                if inside and (b.level or 1) <= level:
                    break
                if not inside and val.lower() in b.text.lower():
                    inside, level = True, b.level or 1
            if inside:
                out.append(b)
        return out, [], f"section '{val}'"
    raise ValueError("part must look like 'page:3', 'pages:3-5', 'slide:5', 'sheet:Name' or 'section:Heading'.")


def document_markdown(doc: Document, part: str | None = None, sheet_rows: int = 30) -> tuple[str, str]:
    blocks, sheets, label = select(doc, part)
    out: list[str] = []
    last_page = None
    for b in blocks:
        p = block_page(b)
        if p is not None and p != last_page:
            out.append(f"<!-- page {p} -->")
            last_page = p
        out.append(_block_md(b))
    for s in sheets:
        out.append(_sheet_md(s, sheet_rows))
    return "\n\n".join(x for x in out if x.strip()), label


def paginate(text: str, page: int, size: int) -> tuple[str, int]:
    """Split on paragraph boundaries into pages of at most `size` characters."""
    if len(text) <= size:
        return text, 1
    pages, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > size and cur:
            pages.append(cur)
            cur = ""
        while len(para) > size:
            pages.append(para[:size])
            para = para[size:]
        cur = (cur + "\n\n" + para) if cur else para
    if cur:
        pages.append(cur)
    page = max(1, min(page, len(pages)))
    return pages[page - 1], len(pages)


def part_matches(saved: str, asked: str | None) -> bool:
    """Whether a saved part ('page:4') falls inside what was asked for ('pages:3-5', 'page:4', or everything)."""
    if not asked:
        return True
    kind, _, val = asked.partition(":")
    kind = kind.strip().lower()
    s_kind, _, s_val = saved.partition(":")
    if kind == "pages" and s_kind == "page" and s_val.isdigit():
        lo, _, hi = val.partition("-")
        return lo.strip().isdigit() and int(lo) <= int(s_val) <= int(hi or lo)
    return saved.lower() == asked.strip().lower()
