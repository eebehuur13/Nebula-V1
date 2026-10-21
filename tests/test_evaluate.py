"""The eval harness points at the layer where an answer goes missing."""

from __future__ import annotations

from helpers import drain
from nebula import access, db, evaluate
from nebula.connectors.base import FetchedItem
from nebula.knowledge import items

ASKER = "eval-asker@example.org"


async def test_eval_reports_the_first_layer_that_misses(mcp):
    asker = await access.ensure_person(ASKER)
    col = await db.fetch_one(
        """insert into collections (slug, name, kind, owner_person) values ('eval-notes', 'Eval notes', 'notebook', %s)
           returning id""", (asker.id,))
    other = await access.ensure_person("eval-other@example.org")
    hidden = await db.fetch_one(
        """insert into collections (slug, name, kind, owner_person) values ('eval-hidden', 'Eval hidden', 'notebook', %s)
           returning id""", (other.id,))
    for cid, title, text in ((col["id"], "Eval handbook", "Teachers in Guntur meet every Saturday morning."),
                             (hidden["id"], "Eval salaries", "Teacher salaries for Guntur, every Saturday paid.")):
        async with db.tx() as conn:
            await items.upsert(conn, str(cid), FetchedItem(external_id=title, title=title, url="", mime_type="text/plain",
                                                           kind="file", data=text.encode(), filename=f"{title}.txt"))
    await drain()

    report = await evaluate.run([
        {"question": "When do teachers in Guntur meet?", "expect": {"title": "Eval handbook", "text": "every Saturday"},
         "must_not_see": ["Eval salaries"]},
        {"question": "When do teachers in Guntur meet?", "expect": {"title": "Eval handbook", "text": "every Sunday"}},
        {"question": "Where is the Mars office?", "expect": {"absent": True}},
        {"question": "Who wrote the Eval report?", "expect": {"title": "Eval report"}},
    ], ASKER)
    first, wrong_text, absent, missing = report["results"]
    assert first["result"] == "pass" and first["checks"]["access"] == "pass"
    assert first["checks"]["retrieval"].startswith("pass: rank")
    assert wrong_text["result"] == "fail" and wrong_text["first_miss"] == "extraction"
    assert absent["result"] == "review"
    assert missing["first_miss"] == "extraction" and "no file" in missing["checks"]["extraction"]
    assert report["summary"] == {"pass": 1, "fail": 2, "review": 1}
