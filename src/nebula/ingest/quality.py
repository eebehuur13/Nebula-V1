"""Cheap checks for what text extraction missed, part by part.

Nebula reads files with plain parsers and doesn't run a model over them. These checks look for the
places where that's likely to have lost something: scanned pages, pages and slides that are mostly a
picture, images and charts whose content isn't in the text, Hindi typed in legacy fonts, and garbled
characters. Each gap names the part (`page:3`, `slide:5`, `image:2`, `sheet:Q2`, or `all`) so Claude
can look at just that part, and says whether it certainly needs reading ("needs_extraction") or
might ("uncertain").

"ok" means none of these checks fired. It's not a guarantee that everything was read correctly.
"""

from __future__ import annotations

import re
from typing import Any

from nebula.ingest.types import Document

NEEDS = "needs_extraction"
UNCERTAIN = "uncertain"
MAX_GAPS = 60
_GARBLED = re.compile("[�-\x00-\x08\x0b\x0c\x0e-\x1f]")
_MOJIBAKE = re.compile("à¤|à¥")  # UTF-8 Devanagari read as Latin-1


def _part(locator: dict[str, Any]) -> str:
    for key in ("page", "slide", "image"):
        if locator.get(key) is not None:
            return f"{key}:{locator[key]}"
    if locator.get("sheet"):
        return f"sheet:{locator['sheet']}"
    return "all"


def assess(doc: Document) -> dict[str, Any]:
    """{"status": ok | uncertain | needs_extraction, "gaps": [{part, state, reason}], "total_gaps": n}"""
    m = doc.metadata
    gaps: list[dict[str, str]] = []
    seen: set[str] = set()

    def gap(part: str, state: str, reason: str) -> None:
        if part not in seen:
            seen.add(part)
            gaps.append({"part": part, "state": state, "reason": reason})

    if m.get("parse_error"):
        gap("all", NEEDS, f"Nebula couldn't read this file ({m['parse_error'][:120]})")
    if m.get("encrypted"):
        gap("all", NEEDS, "the PDF is password-protected")
    if m.get("needs_ocr"):
        for p in range(1, min(int(m.get("page_count") or 1), 30) + 1):
            gap(f"page:{p}", NEEDS, "no text layer: a scanned or image-only page")
    for p in m.get("pages_without_text") or []:
        gap(f"page:{p}", NEEDS, "no text layer: a scanned or image-only page")
    for info in m.get("page_images") or []:
        if info.get("chars", 0) < 300:
            gap(f"page:{info['page']}", NEEDS, "the page is mostly a picture (a scan, an infographic or a photo of a table)")
        else:
            gap(f"page:{info['page']}", UNCERTAIN, "the page has a large image or chart whose content isn't in the text")
    for info in m.get("slide_images") or []:
        if info.get("words", 0) < 25:
            gap(f"slide:{info['slide']}", NEEDS, "the slide is mostly a picture")
        else:
            gap(f"slide:{info['slide']}", UNCERTAIN, "the slide has a large picture whose content isn't in the text")
    for n in m.get("slide_charts") or []:
        gap(f"slide:{n}", UNCERTAIN, "a chart: its title and series names are read, not its values")

    images = m.get("images") or {}
    large = set(images.get("large") or [])
    for b in doc.blocks:
        if b.kind != "image" or b.locator.get("image") is None:
            continue
        n = b.locator["image"]
        if images.get("sized") and n not in large:
            continue  # small: a logo or an icon
        what = f"an image ({b.text})" if b.text else "an image"
        gap(f"image:{n}", UNCERTAIN, f"{what} whose content isn't in the text")

    fonts = m.get("legacy_fonts") or {}
    if fonts.get("unconverted"):
        gap("all", NEEDS, "Hindi typed in a legacy font Nebula can't convert (" + ", ".join(fonts["unconverted"][:3])
            + "): that text came out as gibberish")
    elif fonts.get("kruti") or fonts.get("converted_blocks"):
        gap("all", UNCERTAIN, "Hindi typed in the Kruti Dev font was converted to Unicode; check unusual spellings "
                              "against the original")

    for b in doc.blocks:
        text = b.text or ""
        bad = len(_GARBLED.findall(text))
        if bad >= 5 and bad / max(len(text), 1) >= 0.02:
            gap(_part(b.locator), UNCERTAIN, "garbled characters (a font without a usable character map)")
        elif len(_MOJIBAKE.findall(text)) >= 3:
            gap(_part(b.locator), UNCERTAIN, "Hindi text decoded wrongly (shows as 'à¤…')")
    for sh in doc.sheets:
        if "charts or images" in (sh.notes or ""):
            gap(f"sheet:{sh.name}", UNCERTAIN, "the sheet has charts or images that aren't read")

    if not gaps and not doc.blocks and not doc.sheets and doc.kind in ("pdf", "doc", "slides"):
        gap("all", NEEDS, "no text came out of this file")
    status = NEEDS if any(g["state"] == NEEDS for g in gaps) else UNCERTAIN if gaps else "ok"
    return {"status": status, "gaps": gaps[:MAX_GAPS], "total_gaps": len(gaps)}
