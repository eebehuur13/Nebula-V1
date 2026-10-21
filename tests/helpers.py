"""Shared helpers for the database-backed tests: call a tool as someone, run queued jobs."""

from __future__ import annotations

import json

from fastmcp import Client

from nebula import jobs
from nebula.config import settings

ADMIN = "admin@example.org"
LEAD = "lead@example.org"
MEMBER = "member@example.org"
OUTSIDER = "outsider@example.org"
NEWLEAD = "newlead@example.org"


async def call(mcp, who: str, tool: str, **args):
    from nebula import server

    settings().dev_user_email = who
    server.forget_person()
    async with Client(mcp) as c:
        res = await c.call_tool(tool, args, raise_on_error=False)
    text = res.content[0].text if res.content else ""
    if res.is_error:
        return {"error": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        head, _, rest = text.partition("\n\n")
        try:
            return {**json.loads(head), "_body": rest}
        except json.JSONDecodeError:
            return {"_text": text}


async def raw_call(mcp, who: str, tool: str, **args):
    """The tool result itself, for checking non-text content such as images."""
    from nebula import server

    settings().dev_user_email = who
    server.forget_person()
    async with Client(mcp) as c:
        return await c.call_tool(tool, args, raise_on_error=False)


async def drain() -> None:
    from nebula import worker  # noqa: F401  (registers handlers)

    while await jobs.run_one():
        pass
