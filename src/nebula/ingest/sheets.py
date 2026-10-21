"""Understanding spreadsheets: header row, sheet kind, column types, SQL names, sheet card.

A sheet is either a table of records ("data", loaded into SQL and indexed through
its card), mostly text ("text", each row becomes a search chunk) or something
else ("other": dashboards, pivots, report layouts; card only). Getting this wrong
in the "data" direction produces confident wrong numbers, so every heuristic here
leans towards "other" when unsure.

Values are whatever the parser produced: Python scalars from openpyxl (int,
float, bool, datetime, str) or strings from CSV. All helpers tolerate None,
blanks and junk markers such as "-" or "#N/A".
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, datetime, time
from functools import lru_cache
from collections.abc import Sequence
from typing import Any

from nebula.ingest.types import Sheet, SheetKind

__all__ = [
    "classify_sheet",
    "column_name_mapping",
    "detect_header",
    "format_value",
    "infer_column_types",
    "is_blank",
    "normalize_column_names",
    "sheet_card",
    "to_date",
    "to_number",
]

CARD_MAX_CHARS = 2500
SEP = " › "

# Cell contents that mean "no value". Matched case-insensitively after stripping.
NULL_MARKERS = frozenset(
    {"", "-", "--", "---", "—", "–", ".", "na", "n/a", "n.a.", "#n/a", "nan", "null", "none", "nil",
     "#div/0!", "#ref!", "#value!", "#name?", "#num!", "#null!", "#error!"}
)


# --------------------------------------------------------------------------- values

def is_blank(v: Any) -> bool:
    """True for None, empty/whitespace strings, NaN and common null markers ("-", "NA", "#N/A")."""
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        return not s or (len(s) <= 15 and s.lower() in NULL_MARKERS)
    if isinstance(v, float):
        return math.isnan(v)
    return False


_CURRENCY_RE = re.compile(r"^(?:₹|rs\.?|inr|\$|usd)\s*", re.IGNORECASE)
# Western (1,234,567) or Indian (12,34,567) digit grouping, or plain digits; optional decimals/exponent.
_NUMBER_RE = re.compile(
    r"^(?P<int>\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3}|\d+)?(?P<frac>\.\d+)?(?P<exp>[eE][+-]?\d+)?$"
)


def to_number(v: Any) -> int | float | None:
    """Parse a cell as a number, or None.

    Accepts native ints/floats and strings like "1,234", "1,23,456" (Indian grouping),
    "12.5%", "₹ 1,200", "Rs. 500", "(1,234)" (accounting negative) and "-3.2".
    Percent strings return the number as written ("12%" -> 12.0). Booleans are not numbers.
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if not isinstance(v, str):
        return None
    s = v.replace("\u00a0", " ").strip()
    if not s or len(s) > 40:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s[:1] in "+-":
        negative, s = negative or s[0] == "-", s[1:].lstrip()
    s = _CURRENCY_RE.sub("", s)
    percent = s.endswith("%")
    if percent:
        s = s[:-1].rstrip()
    m = _NUMBER_RE.match(s)
    if not m or not (m.group("int") or m.group("frac")):
        return None
    digits = s.replace(",", "")
    try:
        if m.group("frac") or m.group("exp") or percent:
            num: int | float = float(digits)
        else:
            num = int(digits)
    except ValueError:
        return None
    return -num if negative else num


def _is_integer_value(v: Any, num: int | float) -> bool:
    if isinstance(num, int):
        return True
    # Native floats that hold whole numbers count (Excel stores every number as a double);
    # strings written with a decimal point ("100.0") or percent sign do not.
    return isinstance(v, float) and num.is_integer()


# Cheap pre-filter so text cells don't pay for a dozen strptime attempts.
_DATE_HINT_RE = re.compile(
    r"^(?:\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}[-/ .][A-Za-z]{3,9}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[ ,.'-])",
    re.IGNORECASE,
)
_DATE_FORMATS = (
    "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y", "%d-%b-%y", "%d %b, %Y",
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    "%b %Y", "%B %Y", "%b-%Y", "%B-%Y", "%b-%y", "%b'%y", "%Y/%m/%d",
)


def to_date(v: Any) -> date | datetime | None:
    """Parse a cell as a date or datetime, or None.

    Native date/datetime pass through. Strings: ISO first, then Indian day-first
    forms (01/04/2024 is 1 April), month names ("1 Apr 2024", "April 2024" -> 1 April),
    and finally US month-first as a last resort. Bare numbers are never dates.
    """
    if isinstance(v, datetime | date):
        return v
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not (6 <= len(s) <= 32) or not _DATE_HINT_RE.match(s):
        return None
    return _parse_date_str(s)


_DMY_RE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})$")


@lru_cache(maxsize=65536)  # sheets repeat the same dates and months many times
def _parse_date_str(s: str) -> date | datetime | None:
    m = _DMY_RE.match(s)
    if m:  # fast path for the most common Indian form, day first; month-first only if that fails
        a, b, y = (int(g) for g in m.groups())
        if y < 100:
            y += 2000 if y < 69 else 1900
        for day, month in ((a, b), (b, a)):
            try:
                return date(y, month, day)
            except ValueError:
                continue
        return None
    try:
        d = datetime.fromisoformat(s)
        return d.date() if len(s) <= 10 else d
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return d if "%H" in fmt else d.date()
    return None


_TRUE = frozenset({"true", "yes", "y", "हाँ", "हां"})
_FALSE = frozenset({"false", "no", "n", "नहीं"})


def _to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    return None


def format_value(v: Any, max_len: int | None = None) -> str:
    """Render a cell for humans: ISO dates, trimmed floats, Yes/No for booleans."""
    if v is None:
        s = ""
    elif isinstance(v, bool):
        s = "Yes" if v else "No"
    elif isinstance(v, datetime):
        s = v.date().isoformat() if v.time() == time(0) else v.strftime("%Y-%m-%d %H:%M")
    elif isinstance(v, date):
        s = v.isoformat()
    elif isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            s = str(int(v))
        elif abs(v) >= 1e-3:
            s = f"{v:.4f}".rstrip("0").rstrip(".")
        else:
            s = f"{v:.3g}"
    elif isinstance(v, time):
        s = v.strftime("%H:%M") if not v.second else v.isoformat()
    else:
        s = " ".join(str(v).split())
    if max_len and len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


# --------------------------------------------------------------------------- header

def _row_width(row: Sequence[Any]) -> int:
    """1 + index of the last non-blank cell (0 for an empty row)."""
    for i in range(len(row) - 1, -1, -1):
        if not is_blank(row[i]):
            return i + 1
    return 0


def _filled(row: Sequence[Any]) -> int:
    return sum(1 for c in row if not is_blank(c))


def _is_label(c: Any) -> bool:
    """A cell that reads like a column name: text that isn't a number/date, a date (month
    columns) or a year."""
    if isinstance(c, str):
        return to_number(c) is None
    if isinstance(c, datetime | date):
        return True
    return isinstance(c, int) and not isinstance(c, bool) and 1900 <= c <= 2100


def detect_header(rows: Sequence[Sequence[Any]], scan: int = 20) -> int:
    """Index of the header row within the first `scan` rows.

    The header is the first row that (a) fills at least half of the table width seen in
    the rows below it (so a lone title in A1 is skipped), (b) is mostly labels (non-numeric
    text, or dates/years for month/year columns) and (c) is followed by rows of a
    consistent width. Falls back to the first non-empty row, or 0.
    """
    top = [list(r) for r in rows[: scan + 6]]
    widths = [_row_width(r) for r in top]
    first_nonblank = next((i for i, w in enumerate(widths) if w), 0)
    for i in range(min(scan, len(top))):
        w = widths[i]
        if not w:
            continue
        cells = [c for c in top[i][:w] if not is_blank(c)]
        below = [j for j in range(i + 1, len(top)) if widths[j]][:5]
        table_w = max([w] + [widths[j] for j in below])
        if len(cells) < (1 if table_w == 1 else max(2, 0.5 * table_w)):
            continue
        if sum(_is_label(c) for c in cells) < 0.8 * len(cells):
            continue
        if not below:
            return i
        consistent = sum(
            1
            for j in below
            if widths[j] <= w + 1 and _filled(top[j]) >= max(1, 0.3 * len(cells))
        )
        if consistent >= 0.6 * len(below):
            return i
    return first_nonblank


# --------------------------------------------------------------------------- classification

_PIVOT_MARKERS = ("row labels", "column labels", "grand total", "sum of ", "count of ", "average of ")
_LAYOUT_NAME_RE = re.compile(r"dashboard|pivot|summary|chart|graph|calc|report|analysis|overview", re.I)
_SENTENCE_BREAK_RE = re.compile(r"[.!?।]\s+\S")


def _is_value_like(c: Any) -> bool:
    """Numbers, dates, booleans (native or written as text)."""
    if isinstance(c, bool | int | float | datetime | date | time):
        return True
    return to_number(c) is not None or to_date(c) is not None or _to_bool(c) is not None


def classify_sheet(
    name: str,
    header: Sequence[Any],
    rows: Sequence[Sequence[Any]],
    has_formulas: bool | float,
) -> SheetKind:
    """Decide whether a sheet is "data", "text" or "other".

    `has_formulas` is either a flag or the fraction (0-1) of non-empty cells that are
    formulas. Rules, applied in order on the first 2,000 body rows:

    1. No body rows, or a header that is less than half filled -> "other".
    2. Formula fraction >= 0.5, or pivot-table markers ("Row Labels", "Grand Total",
       "Sum of ...") in the header / first column -> "other".
    3. Mostly text: text cells are at least half of the filled cells and either average
       > 40 chars or >= 25% of them are prose (multi-sentence, or 12+ words) -> "text".
       Record tables with long names or addresses but no prose stay "data".
    4. Layout signals -> "other": fill density < 35%; > 20% of rows spill past the header;
       an interior header gap over an empty column (side-by-side tables); a header-like row
       of labels after a blank row inside the body (stacked tables); or a dashboard-ish
       sheet name ("Dashboard", "Summary", "Pivot", ...) combined with any formulas.
    5. Otherwise, >= 3 non-empty rows -> "data"; fewer -> "other".
    """
    width = _row_width(header)
    body = [list(r) for r in rows[:2000]]
    nonblank = [r for r in body if _filled(r)]
    header_filled = _filled(header[:width])
    if not nonblank or not width or header_filled < 0.5 * width:
        return "other"

    if isinstance(has_formulas, bool):
        formula_frac, any_formulas = (0.0, has_formulas)
    else:
        formula_frac, any_formulas = (float(has_formulas), has_formulas > 0)
    if formula_frac >= 0.5:
        return "other"

    first_col = [str(r[0]).strip().lower() for r in nonblank if r and isinstance(r[0], str)]
    labels = [str(h).strip().lower() for h in header if isinstance(h, str)] + first_col
    if any(lbl.startswith(m) for lbl in labels for m in _PIVOT_MARKERS):
        return "other"

    # Cell statistics over the header width.
    filled = text_cells = prose_cells = text_len = 0
    col_numeric = [0] * width
    col_filled = [0] * width
    for r in nonblank:
        for j, c in enumerate(r[:width]):
            if is_blank(c):
                continue
            filled += 1
            col_filled[j] += 1
            if _is_value_like(c):
                col_numeric[j] += 1
            else:
                s = str(c).strip()
                text_cells += 1
                text_len += len(s)
                if _SENTENCE_BREAK_RE.search(s) or len(s.split()) >= 12:
                    prose_cells += 1
    if filled == 0:
        return "other"

    if text_cells >= 0.5 * filled and (text_len / text_cells > 40 or prose_cells >= 0.25 * text_cells):
        return "text"

    density = filled / (len(nonblank) * width)
    spill = sum(1 for r in nonblank if _row_width(r) > width + 1)
    if density < 0.35 or spill > 0.2 * len(nonblank):
        return "other"

    for j in range(1, width - 1):
        if is_blank(header[j]) and col_filled[j] <= 0.1 * len(nonblank):
            return "other"

    numeric_cols = [j for j in range(width) if col_filled[j] and col_numeric[j] >= 0.8 * col_filled[j]]
    if numeric_cols:
        prev_blank = False
        for r in body:
            if not _filled(r):
                prev_blank = True
                continue
            if prev_blank and _filled(r[:width]) >= 0.5 * width:
                cells = [c for c in r[:width] if not is_blank(c)]
                if all(isinstance(c, str) and not _is_value_like(c) for c in cells) and any(
                    j < len(r) and isinstance(r[j], str) and not is_blank(r[j]) for j in numeric_cols
                ):
                    return "other"
            prev_blank = False

    if any_formulas and _LAYOUT_NAME_RE.search(name or ""):
        return "other"

    return "data" if len(nonblank) >= 3 else "other"


# --------------------------------------------------------------------------- column types

def infer_column_types(sheet: Sheet) -> list[str]:
    """One of "integer", "numeric", "date", "timestamp", "boolean", "text" per column.

    A column gets a non-text type only if *every* non-blank value fits it (blanks and
    null markers like "-" or "#N/A" are ignored), so a SQL load never fails a cast.
    Numbers written as text ("1,234", "1,23,456", "12%", "₹500") count as numeric.
    Dates become "timestamp" when any value carries a time of day. A column with no
    values at all is "text".
    """
    n = max(sheet.n_cols, len(sheet.header), max((len(r) for r in sheet.rows[:1]), default=0))
    types: list[str] = []
    for j in range(n):
        boolean = integer = numeric = temporal = True
        has_time = False
        seen = 0
        for row in sheet.rows:
            v = row[j] if j < len(row) else None
            if is_blank(v):
                continue
            seen += 1
            if boolean and _to_bool(v) is None:
                boolean = False
            if numeric:
                num = to_number(v)
                if num is None:
                    numeric = integer = False
                elif integer and not _is_integer_value(v, num):
                    integer = False
            if temporal:
                d = to_date(v)
                if d is None:
                    temporal = False
                elif isinstance(d, datetime) and d.time() != time(0):
                    has_time = True
            if not (boolean or numeric or temporal):
                break
        if not seen:
            types.append("text")
        elif boolean:
            types.append("boolean")
        elif integer:
            types.append("integer")
        elif numeric:
            types.append("numeric")
        elif temporal:
            types.append("timestamp" if has_time else "date")
        else:
            types.append("text")
    return types


# --------------------------------------------------------------------------- column names

# Postgres reserved words that cannot be used as bare column names.
_RESERVED = frozenset(
    "all analyse analyze and any array as asc asymmetric authorization between binary both case cast "
    "check collate column concurrently constraint create cross current_catalog current_date current_role "
    "current_schema current_time current_timestamp current_user default deferrable desc distinct do else "
    "end except false fetch for foreign freeze from full grant group having ilike in initially inner "
    "intersect into is isnull join lateral leading left like limit localtime localtimestamp natural not "
    "notnull null offset on only or order outer overlaps placing primary references returning right "
    "select session_user similar some symmetric table tablesample then to trailing true union unique "
    "user using variadic verbose when where window with".split()
)
_MAX_IDENT = 60


def _snake(text: str) -> str:
    s = text.replace("%", " pct ").replace("&", " and ").replace("#", " num ").replace("₹", " inr ")
    s = s.encode("ascii", "ignore").decode()
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)  # camelCase -> camel_case
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s


def normalize_column_names(header: Sequence[Any]) -> list[str]:
    """Safe, unique snake_case SQL identifiers, aligned with `header`.

    ASCII only and nothing is transliterated: empty headers and non-ASCII headers
    without a usable ASCII part (e.g. Hindi "जिला") become col_<n> (1-based position);
    mixed headers keep their ASCII part ("जिला / District" -> "district"). Names never start with a digit, are
    at most 60 chars, avoid Postgres reserved words (suffix "_") and are de-duplicated
    with _2, _3. Use `column_name_mapping` to keep the originals.
    """
    out: list[str] = []
    used: set[str] = set()
    for i, h in enumerate(header):
        raw = "" if h is None else format_value(h)
        s = _snake(raw)
        if not s or (not raw.isascii() and sum(ch.isalpha() for ch in s) < 2):
            s = f"col_{i + 1}"
        if s[0].isdigit():
            s = "c_" + s
        s = s[:_MAX_IDENT].rstrip("_")
        if s in _RESERVED:
            s += "_"
        base, k = s, 2
        while s in used:
            suffix = f"_{k}"
            s = base[: _MAX_IDENT - len(suffix)].rstrip("_") + suffix
            k += 1
        used.add(s)
        out.append(s)
    return out


def column_name_mapping(header: Sequence[Any]) -> dict[str, str]:
    """SQL name -> original header text, in column order."""
    names = normalize_column_names(header)
    return {name: ("" if h is None else format_value(h)) for name, h in zip(names, header, strict=True)}


# --------------------------------------------------------------------------- sheet card

_KIND_LINE = {
    "data": "Data table, queryable as SQL: {rows} rows × {cols} columns.",
    "text": "Text sheet (each row is indexed as its own entry): {rows} rows × {cols} columns.",
    "other": "Dashboard / report layout, not a plain table (open the original file for details): "
             "{rows} rows × {cols} columns.",
}
_PERIOD_NAME_RE = re.compile(r"\b(year|yr|fy|session)\b", re.I)


def _fmt_num(v: int | float) -> str:
    if isinstance(v, int) or float(v).is_integer():
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else str(round(v))


def _fmt_date(d: date | datetime, timestamp: bool) -> str:
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d %H:%M") if timestamp and d.time() != time(0) else d.date().isoformat()
    return d.isoformat()


def _sort_key(d: date | datetime) -> datetime:
    return d if isinstance(d, datetime) else datetime(d.year, d.month, d.day)


def _column_summary(values: list[Any], ctype: str) -> tuple[str, Any]:
    """(summary text, (min, max) range or None) for one column's non-blank values."""
    if not values:
        return "empty", None
    if ctype in ("integer", "numeric"):
        nums = [n for n in (to_number(v) for v in values) if n is not None]
        if nums:
            lo, hi = min(nums), max(nums)
            return (f"{_fmt_num(lo)} to {_fmt_num(hi)}" if lo != hi else _fmt_num(lo)), (lo, hi)
    if ctype in ("date", "timestamp"):
        ds = [d for d in (to_date(v) for v in values) if d is not None]
        if ds:
            lo, hi = min(ds, key=_sort_key), max(ds, key=_sort_key)
            ts = ctype == "timestamp"
            return f"{_fmt_date(lo, ts)} to {_fmt_date(hi, ts)}", (lo, hi)
    if ctype == "boolean":
        counts = Counter(_to_bool(v) for v in values)
        return f"Yes {counts[True]}, No {counts[False]}", None
    counts = Counter(format_value(v, 32) for v in values)
    examples = ", ".join(v for v, _ in counts.most_common(4))
    more = ", …" if len(counts) > 4 else ""
    return f"{len(counts)} distinct: {examples}{more}", None


def _render_row(header: Sequence[str], row: Sequence[Any], max_len: int = 320) -> str:
    parts = [f"{h}: {format_value(v, 40)}" for h, v in zip(header, row, strict=False) if not is_blank(v)]
    s = "; ".join(parts)
    return s if len(s) <= max_len else s[: max_len - 1].rstrip() + "…"


def _header_names(sheet: Sheet, n: int) -> list[str]:
    names = []
    for j in range(n):
        h = sheet.header[j] if j < len(sheet.header) else None
        names.append(format_value(h, 60) if not is_blank(h) else f"Column {j + 1}")
    return names


def sheet_card(sheet: Sheet, doc_title: str, column_types: list[str] | None = None,
               max_chars: int = CARD_MAX_CHARS) -> str:
    """The short text indexed for a sheet instead of its cells.

    First line is the breadcrumb "Doc title › Sheet". Then the kind and size, the period
    covered (range of the first date column, or of a year column), every column with its
    type and either a range (numbers, dates) or the most common values (text), three
    sample rows (first, middle, last) and the parser's notes. Dashboard-like ("other")
    sheets list their labels instead, so no numbers go into the search index. Stays under
    `max_chars` (default 2,500).
    """
    budget = max(100, max_chars)
    n_cols = max(sheet.n_cols, len(sheet.header))
    names = _header_names(sheet, n_cols)
    sql_names = normalize_column_names([*sheet.header, *[None] * (n_cols - len(sheet.header))][:n_cols])
    nonblank_rows = [r for r in sheet.rows if any(not is_blank(c) for c in r)]
    lines = [
        f"{doc_title or 'Untitled'}{SEP}{sheet.name}",
        _KIND_LINE.get(sheet.kind, _KIND_LINE["other"]).format(rows=f"{len(nonblank_rows):,}", cols=n_cols),
    ]
    if sheet.has_formulas:
        lines[-1] += " Contains formulas."
    tail: list[str] = []
    if sheet.notes:
        tail.append("Notes: " + format_value(sheet.notes, 400))

    if sheet.kind == "other":
        seen: dict[str, None] = {}
        for c in [*sheet.header, *(c for r in sheet.rows[:500] for c in r)]:
            if isinstance(c, str) and not is_blank(c) and to_number(c) is None:
                seen.setdefault(format_value(c, 50), None)
            if len(seen) >= 40:
                break
        if seen:
            lines.append("Labels: " + ", ".join(seen))
        return _fit(lines, tail, budget)

    types = column_types or infer_column_types(sheet)
    col_lines: list[str] = []
    period: str | None = None
    for j in range(n_cols):
        ctype = types[j] if j < len(types) else "text"
        values = [r[j] for r in nonblank_rows if j < len(r) and not is_blank(r[j])]
        summary, rng = _column_summary(values, ctype)
        obvious = "_".join(names[j].lower().split())  # only spell out SQL names that aren't guessable
        label = names[j] if sql_names[j] == obvious else f"{names[j]} [{sql_names[j]}]"
        empty_share = 1 - len(values) / len(nonblank_rows) if nonblank_rows else 0
        extra = f", {empty_share:.0%} empty" if values and empty_share >= 0.5 else ""
        col_lines.append(f"- {label} ({ctype}{extra}): {summary}")
        is_years = ctype == "integer" and _PERIOD_NAME_RE.search(names[j]) and rng and 1900 <= rng[0] <= rng[1] <= 2100
        if period is None and rng is not None and (ctype in ("date", "timestamp") or is_years):
            period = f"Period covered: {summary} ({names[j]})"
    if period:
        lines.append(period)

    # Priority when space runs out: columns, then notes, then sample rows.
    tail_len = sum(len(t) + 1 for t in tail)
    used = sum(len(s) + 1 for s in lines) + len("Columns:") + 1
    col_room = budget - tail_len - min(200, budget // 6)
    lines.append("Columns:")
    for k, cl in enumerate(col_lines):
        last = k == len(col_lines) - 1
        if used + len(cl) + 1 > col_room - (0 if last else 150):  # keep room to name the rest
            more = f"- … and {n_cols - k} more: " + ", ".join(names[k:])
            room = max(30, col_room - used - 1)
            if len(more) > room:
                more = more[: more.rfind(", ", 0, room - 2)] + ", …"
            lines.append(more)
            used += len(more) + 1
            break
        lines.append(cl)
        used += len(cl) + 1
    if nonblank_rows:
        picks = sorted({0, len(nonblank_rows) // 2, len(nonblank_rows) - 1})
        samples = [f"- {_render_row(names, nonblank_rows[i])}" for i in picks]
        room = budget - tail_len - used - len("Sample rows:") - 1
        fitting = []
        for sample in samples:
            if len(sample) + 1 > room:
                break
            fitting.append(sample)
            room -= len(sample) + 1
        if fitting:
            lines.append("Sample rows:")
            lines.extend(fitting)
    return _fit(lines, tail, budget)


def _fit(lines: list[str], tail: list[str], budget: int) -> str:
    """Join lines + tail, dropping from the end of `lines` then hard-trimming to fit."""
    text = "\n".join(lines + tail)
    while len(text) > budget and len(lines) > 2:
        lines.pop()
        text = "\n".join(lines + tail)
    if len(text) > budget:
        text = text[: budget - 1].rstrip() + "…"
    return text
