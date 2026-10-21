"""`nebula` command: serve, worker, migrate, and a few admin helpers for setup."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nebula", description="Nebula: Rocket Learning's memory for Claude")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Run the MCP server and web routes")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("worker", help="Run the background worker")
    sub.add_parser("migrate", help="Apply database migrations")
    a = sub.add_parser("admin", help="Make someone a Nebula admin")
    a.add_argument("email")
    g = sub.add_parser("group", help="Create a group (team)")
    g.add_argument("id")
    g.add_argument("name")
    g.add_argument("--lead")
    m = sub.add_parser("member", help="Add someone to a group")
    m.add_argument("group")
    m.add_argument("email")
    m.add_argument("--lead", action="store_true")
    c = sub.add_parser("collection", help="Register a source collection")
    c.add_argument("slug")
    c.add_argument("name")
    c.add_argument("kind", choices=["gdrive", "notion", "granola", "notebook"])
    c.add_argument("owner_group")
    c.add_argument("--config", default="{}", help="JSON source config")
    c.add_argument("--audience", nargs="*", default=[])
    d = sub.add_parser("connect-drive", help="Connect a Drive folder on someone's behalf (access follows Drive)")
    d.add_argument("email", help="Who manages the connection")
    d.add_argument("folder", help="Drive folder link or id")
    d.add_argument("--name")
    sub.add_parser("run-jobs", help="Run queued jobs until the queue is empty, then exit")
    e = sub.add_parser("eval", help="Check extraction and retrieval against known answers (see nebula.evaluate)")
    e.add_argument("cases", help="JSON file: a list of cases")
    e.add_argument("--as", dest="person", default="", help="Who asks, for cases that don't say")
    e.add_argument("--out", help="Also write the full results to this JSON file")
    sub.add_parser("discover", help="Register anything newly shared with Nebula in Drive, now")
    pd = sub.add_parser("probe-drive", help="Read-only checks of the service account's Drive access (see drive_probe)")
    pd.add_argument("folder", help="A shared folder's link or id")
    pd.add_argument("--watch", type=int, default=0, help="Seconds to watch the change feed for an edit")
    args = p.parse_args(argv)
    _setup_logging()

    if args.cmd == "serve":
        import uvicorn

        from nebula.app import build_app

        uvicorn.run(build_app(), host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*",
                    timeout_keep_alive=65)
        return
    if args.cmd == "worker":
        from nebula.worker import run_forever

        asyncio.run(run_forever())
        return
    if args.cmd == "probe-drive":
        from nebula.drive_probe import main as probe

        asyncio.run(probe(args.folder, args.watch))
        return
    asyncio.run(_admin(args))


async def _admin(args: argparse.Namespace) -> None:
    from nebula import access, db, org

    await db.migrate()
    try:
        if args.cmd == "migrate":
            print("migrations up to date")
            return
        if args.cmd == "admin":
            await db.execute(
                "insert into people (email, is_admin) values (%s, true) on conflict (email) do update set is_admin = true",
                (args.email.lower(),),
            )
            print(f"{args.email} is now a Nebula admin")
            return
        system = access.Person(id="00000000-0000-0000-0000-000000000000", email="system", name="system",
                               is_admin=True)
        if args.cmd == "group":
            print(json.dumps(await org.create_group(system, args.id, args.name, lead_email=args.lead)))
        elif args.cmd == "member":
            print(json.dumps(await org.set_member(system, args.group, args.email, "lead" if args.lead else "member")))
        elif args.cmd == "collection":
            await db.execute("insert into people (id, email, name, status) values (%s,'system','system','active') "
                             "on conflict do nothing", (system.id,))
            print(json.dumps(await org.create_collection(
                system, slug=args.slug, name=args.name, kind=args.kind, source_config=json.loads(args.config),
                owner_group=args.owner_group, audience=args.audience)))
        elif args.cmd == "connect-drive":
            person = await access.ensure_person(args.email)
            print(json.dumps(await org.connect_drive(person, args.folder, args.name), default=str))
        elif args.cmd == "eval":
            from pathlib import Path

            from nebula import evaluate

            cases = json.loads(Path(args.cases).read_text())
            report = await evaluate.run(cases, args.person)
            if args.out:
                Path(args.out).write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str))
            for r in report["results"]:
                print(f"{r['result']:8} {r['question'][:70]}" + (f"  (first miss: {r['first_miss']})"
                                                                 if r.get("first_miss") else ""))
            print(json.dumps({k: report[k] for k in ("cases", "summary", "first_miss_by_layer")}))
        elif args.cmd == "discover":
            from nebula import sources

            print(json.dumps(await sources.discover(), default=str))
        elif args.cmd == "run-jobs":
            from nebula import jobs
            from nebula import worker  # noqa: F401  registers handlers

            n = 0
            while await jobs.run_one():
                n += 1
            print(f"ran {n} jobs")
    finally:
        await db.close()


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
