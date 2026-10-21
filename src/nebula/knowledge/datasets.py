"""Spreadsheets that are tables of data become real SQL tables in schema `data`.

The search index only gets the sheet's card (what it is, columns, ranges, period);
number questions are answered with SQL through the `query` tool.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg import AsyncConnection, sql

from nebula import access, db
from nebula.ingest.types import Chunk, Sheet

PG_TYPES = {
    "integer": "bigint",
    "numeric": "numeric",
    "date": "date",
    "timestamp": "timestamp",
    "boolean": "boolean",
    "text": "text",
}

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %Y", "%B %Y",
                 "%m/%d/%Y", "%Y/%m/%d")


def _num(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return Decimal(int(v))
    if isinstance(v, (int, float, Decimal)):
        try:
            return Decimal(str(v))
        except InvalidOperation:
            return None
    s = str(v).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d / 100 if pct else d


def _date(v: Any) -> dt.date | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    for f in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def _ts(v: Any) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    d = _date(v)
    if d:
        return dt.datetime(d.year, d.month, d.day)
    try:
        return dt.datetime.fromisoformat(str(v).strip()).replace(tzinfo=None)
    except ValueError:
        return None


def _bool(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1", "हाँ", "हां"):
        return True
    if s in ("false", "no", "n", "0", "नहीं"):
        return False
    return None


def convert(value: Any, typ: str) -> Any:
    if typ == "integer":
        d = _num(value)
        if d is None:
            return None
        return int(d) if d == d.to_integral_value() and abs(d) < 2**62 else None
    if typ == "numeric":
        return _num(value)
    if typ == "date":
        return _date(value)
    if typ == "timestamp":
        return _ts(value)
    if typ == "boolean":
        return _bool(value)
    if value is None:
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def table_name_for(item_id: str, sheet_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", sheet_name.lower()).strip("_")[:24] or "sheet"
    return f"t_{item_id.replace('-', '')[:12]}_{slug}"


async def load_sheet(
    conn: AsyncConnection,
    *,
    collection_id: str,
    item_id: str,
    doc_title: str,
    sheet: Sheet,
) -> tuple[str, Chunk]:
    """(Re)create the SQL table for one data sheet and its dataset row. Returns (dataset id, card chunk)."""
    from nebula.ingest import infer_column_types, normalize_column_names, sheet_card

    types = infer_column_types(sheet)
    names = normalize_column_names(sheet.header)
    table = table_name_for(item_id, sheet.name)
    ident = sql.Identifier("data", table)

    await conn.execute(sql.SQL("drop table if exists {}").format(ident))
    cols = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL(PG_TYPES.get(t, "text"))) for n, t in zip(names, types)
    )
    await conn.execute(sql.SQL("create table {} ({})").format(ident, cols))
    await conn.execute(sql.SQL("grant select on {} to nebula_reader").format(ident))

    width = len(names)
    # The parser keeps blank rows so spreadsheet row numbers stay right; they must not become
    # rows of NULLs in the table (they would inflate every count).
    data_rows = [r for r in sheet.rows if any(v not in (None, "") and str(v).strip() not in ("", "-", "#N/A")
                                               for v in r[:width])]
    copy_sql = sql.SQL("copy {} ({}) from stdin").format(ident, sql.SQL(", ").join(map(sql.Identifier, names)))
    async with conn.cursor() as cur:
        async with cur.copy(copy_sql) as copy:
            for row in data_rows:
                vals = list(row[:width]) + [None] * (width - len(row))
                await copy.write_row([convert(v, t) for v, t in zip(vals, types)])

    card = sheet_card(sheet, doc_title, types)
    card += f"\n\nSQL table: data.{table} (use the `query` tool)."
    columns = [{"name": n, "original": o, "type": t} for n, o, t in zip(names, sheet.header, types)]
    row = await db.fetch_one(
        """
        insert into datasets (collection_id, item_id, sheet, table_name, title, card, columns, row_count, status)
        values (%s, %s, %s, %s, %s, %s, %s, %s, 'active')
        on conflict (item_id, sheet) do update set
            table_name = excluded.table_name, title = excluded.title, card = excluded.card,
            columns = excluded.columns, row_count = excluded.row_count, status = 'active', updated_at = now()
        returning id
        """,
        (collection_id, item_id, sheet.name, table, f"{doc_title} › {sheet.name}", card,
         db.jsonb(columns), len(data_rows)),
        conn,
    )
    assert row is not None
    chunk = Chunk(text=card, heading_path=[doc_title, sheet.name], locator={"sheet": sheet.name}, kind="card")
    return str(row["id"]), chunk


async def drop_for_item(conn: AsyncConnection, item_id: str, keep_sheets: set[str] | None = None) -> None:
    rows = await db.fetch_all("select id, sheet, table_name from datasets where item_id = %s", (item_id,), conn)
    for r in rows:
        if keep_sheets is not None and r["sheet"] in keep_sheets:
            continue
        await conn.execute(sql.SQL("drop table if exists {}").format(sql.Identifier("data", r["table_name"])))
        await conn.execute("delete from chunks where source_type = 'dataset' and source_id = %s", (r["id"],))
        await conn.execute("delete from datasets where id = %s", (r["id"],))


# The `query` tool ---------------------------------------------------------------------

_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|copy|call|do|"
                        r"set|reset|lock|vacuum|analyze|refresh|listen|notify|prepare|execute)\b", re.I)


class QueryError(Exception):
    pass


def _relations(plan: Any) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "Relation Name" in node:
                found.add((node.get("Schema", ""), node["Relation Name"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    return found


async def run_query(person: access.Person, query: str, max_rows: int = 200) -> dict:
    q = query.strip().rstrip(";").strip()
    if ";" in q:
        raise QueryError("Send one SQL statement at a time.")
    if not re.match(r"^(select|with)\b", q, re.I) or _FORBIDDEN.search(re.sub(r"'[^']*'", "''", q)):
        raise QueryError("Only read-only SELECT queries are allowed.")
    max_rows = max(1, min(max_rows, 2000))
    cols, extras = await access.visible_scope(person)
    allowed = await db.fetch_all(
        """select table_name from datasets where status = 'active'
           and (collection_id = any(%s::uuid[]) or id = any(%s::uuid[]))""",
        (cols, [rid for (t, rid) in extras if t == "dataset"]),
    )
    allowed_tables = {r["table_name"] for r in allowed}
    async with db.tx() as conn:
        await conn.execute("set transaction read only")
        await conn.execute("set local statement_timeout = '15s'")
        await conn.execute("set local role nebula_reader")
        try:
            cur = await conn.execute(f"explain (format json, verbose) {q}")  # noqa: S608 (validated above, read-only role)
            plan = (await cur.fetchone())
        except Exception as e:
            raise QueryError(f"SQL error: {e}") from e
        plan_json = plan["QUERY PLAN"] if isinstance(plan, dict) else plan[0]
        if isinstance(plan_json, str):
            plan_json = json.loads(plan_json)
        for schema, rel in _relations(plan_json):
            if schema != "data" or rel not in allowed_tables:
                raise QueryError(f"You can't query {schema}.{rel}. Find datasets with `search` first.")
        try:
            cur = await conn.execute(f"select * from ({q}) as nebula_q limit {max_rows + 1}")  # noqa: S608
            rows = await cur.fetchall()
        except Exception as e:
            raise QueryError(f"SQL error: {e}") from e
        names = [d.name for d in cur.description or []]
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {"columns": names, "rows": [[_jsonable(r[n]) for n in names] for r in rows], "truncated": truncated}


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v
