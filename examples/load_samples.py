"""Load the sample documents into a local Nebula, so search works straight away.

    uv run python examples/load_samples.py

They go into a notebook called "Sample documents", owned by NEBULA_DEV_USER_EMAIL (the person every
request acts as in dev mode). The Hindi notice is typed in the old Kruti Dev font, so you can see
it converted; the CSV becomes a table you can query with SQL.
"""

import asyncio
from pathlib import Path

from nebula import access, db, jobs
from nebula import worker  # noqa: F401  (registers the background job handlers)
from nebula.config import settings
from nebula.connectors.base import FetchedItem
from nebula.knowledge import items

SAMPLES = Path(__file__).parent / "sample-docs"
TITLES = {
    "programme-review.md": "Programme review, October",
    "centre-attendance.csv": "Centre attendance by district",
    "district-notice-krutidev.txt": "District notice (Hindi)",
}


async def main() -> None:
    await db.migrate()
    person = await access.ensure_person(settings().dev_user_email, "Dev User")
    notebook = await db.fetch_one(
        """insert into collections (slug, name, kind, owner_person) values ('samples', 'Sample documents', 'notebook', %s)
           on conflict (slug) do update set name = excluded.name returning id""", (person.id,))
    for path in sorted(SAMPLES.iterdir()):
        fetched = FetchedItem(external_id=f"sample:{path.name}", title=TITLES.get(path.name, path.stem), url="",
                              mime_type="", kind="upload", data=path.read_bytes(), filename=path.name)
        async with db.tx() as conn:
            res = await items.upsert(conn, str(notebook["id"]), fetched, created_by=person.id)
        print(f"loaded {fetched.title!r} as item:{res['id']}")
    ran = 0
    while await jobs.run_one():  # embed everything now instead of waiting for the worker
        ran += 1
    print(f"indexed ({ran} background jobs run). Owner: {person.email}")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
