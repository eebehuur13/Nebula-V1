"""Check extraction and retrieval against known answers, layer by layer, without spending Claude tokens.

A case is a question someone at RL would ask, who asks it, and what a good answer needs:

    {"question": "When do teachers in Guntur meet?", "as": "priya@example.org",
     "expect": {"title": "Teacher handbook", "text": "every Saturday"},
     "must_not_see": ["Salary sheet"]}

    {"question": "Total active centres in Krishna in August?", "as": "...",
     "sql": "select sum(active_centres) from data.t_... where district='Krishna' and month='2026-08-01'",
     "expect": {"rows": [[390]]}}

    {"question": "What's our Mars office address?", "expect": {"absent": true}}

For each case Nebula checks, in order, where a miss happens:
  extraction  is the expected text in the stored, structured file at all?
  retrieval   does search (as that person) return the file, and how high?
  excerpt     does the returned excerpt (or whole short file) contain the expected text?
  access      are files the person mustn't see kept out?
  numbers     does the SQL give the expected rows?
Absent-answer cases are marked "review" with what came back, since only a person can judge them.
Claude's own answer quality is checked separately, in real conversations through the connector.
"""

from __future__ import annotations

import json
import time
from typing import Any

from nebula import access, db
from nebula.knowledge import datasets, items, render, search as search_mod


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


async def _item_text(title: str) -> tuple[str | None, str]:
    """The structured text of the newest active item with this title (ignoring access), or None."""
    row = await db.fetch_one(
        "select * from items where lower(title)=lower(%s) and status='active' order by updated_at desc limit 1", (title,))
    if not row:
        return None, ""
    doc = items.document_of(row)
    text = render.document_markdown(doc)[0] if doc else ""
    extra = "\n".join(t["text"] for t in await items.transcriptions(None, str(row["id"])))
    return str(row["id"]), text + "\n" + extra


async def run_case(case: dict[str, Any], default_person: str) -> dict[str, Any]:
    who = (case.get("as") or default_person).lower()
    person = await access.get_person(who)
    out: dict[str, Any] = {"question": case["question"], "as": who, "checks": {}}
    if person is None:
        out["result"] = "error"
        out["why"] = f"no Nebula account for {who}"
        return out
    expect = case.get("expect") or {}
    checks = out["checks"]

    if expect.get("title"):
        item_id, text = await _item_text(expect["title"])
        if item_id is None:
            checks["extraction"] = "fail: no file with that title is in Nebula"
        elif expect.get("text") and _norm(expect["text"]) not in _norm(text):
            checks["extraction"] = "fail: the file is in Nebula but its extracted text lacks the expected words"
        else:
            checks["extraction"] = "pass"

    started = time.perf_counter()
    found = await search_mod.search(person, case["question"], limit=case.get("limit") or 8)
    out["search_ms"] = round((time.perf_counter() - started) * 1000)
    results = found.get("results", [])
    out["top"] = [r.get("title") for r in results[:5]]
    out["result_chars"] = len(json.dumps(found, ensure_ascii=False, default=str))

    if expect.get("title"):
        rank = next((i + 1 for i, r in enumerate(results) if _norm(r.get("title", "")) == _norm(expect["title"])), None)
        checks["retrieval"] = f"pass: rank {rank}" if rank else "fail: not in the results"
        if rank and expect.get("text"):
            hit = results[rank - 1]
            shown = json.dumps(hit, ensure_ascii=False, default=str)
            checks["excerpt"] = ("pass" if _norm(expect["text"]) in _norm(shown)
                                 else "partial: found the file, but the excerpt doesn't show the answer (open needed)")
    for title in case.get("must_not_see") or []:
        leaked = any(_norm(r.get("title", "")) == _norm(title) for r in results)
        checks.setdefault("access", "pass")
        if leaked:
            checks["access"] = f"FAIL: '{title}' came back for someone who mustn't see it"
    if case.get("sql"):
        try:
            got = await datasets.run_query(person, case["sql"], 200)
            rows = got.get("rows")
            checks["numbers"] = "pass" if rows == expect.get("rows") else f"fail: got {rows}"
        except Exception as e:
            checks["numbers"] = f"fail: {e}"
    if expect.get("absent"):
        checks["absent"] = "review: Nebula should say it doesn't know; check these don't answer it: " + \
                           ", ".join(map(str, out["top"][:3]) or ["(nothing came back)"])

    states = [v.split(":", 1)[0].lower() for v in checks.values()]
    out["result"] = ("fail" if "fail" in states else "partial" if "partial" in states
                     else "review" if "review" in states else "pass")
    first_miss = next((k for k, v in checks.items() if not v.startswith(("pass", "review"))), None)
    if first_miss:
        out["first_miss"] = first_miss
    return out


async def run(cases: list[dict[str, Any]], default_person: str) -> dict[str, Any]:
    results = [await run_case(c, default_person) for c in cases]
    summary: dict[str, int] = {}
    misses: dict[str, int] = {}
    for r in results:
        summary[r["result"]] = summary.get(r["result"], 0) + 1
        if r.get("first_miss"):
            misses[r["first_miss"]] = misses.get(r["first_miss"], 0) + 1
    return {"cases": len(results), "summary": summary, "first_miss_by_layer": misses, "results": results}
