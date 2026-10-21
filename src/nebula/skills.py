"""RL's shared skills (Agent Skills format: SKILL.md plus files).

Individual Claude plans can't share skills across an organisation, so Nebula stores
them with versions, an owner and an audience, and Claude loads them on demand.
A draft is visible to its owners; publishing makes it visible to its audience
(the owner group by default, or whoever it is shared with).
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from nebula import access, db, storage
from nebula.ingest.types import Chunk
from nebula.knowledge import index

NAME = re.compile(r"^[a-z0-9-]{2,64}$")
INLINE_LIMIT = 20_000


def _frontmatter(skill_md: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", skill_md, re.S)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1)) or {}
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def _chunk(name: str, description: str, skill_md: str) -> Chunk:
    body = re.sub(r"^---.*?---\s*", "", skill_md, flags=re.S)
    return Chunk(text=f"Skill {name}: {description}\n{body[:1500]}", heading_path=[f"skill {name}"], kind="text")


async def visible(person: access.Person, query: str | None = None, limit: int = 15) -> list[dict]:
    """Skills the person can use (published and shared with them) plus their own drafts."""
    rows = await db.fetch_all(
        """select s.*, (select count(*) from skill_feedback f where f.skill_id = s.id) as feedback
           from skills s where s.status <> 'retired' order by s.updated_at desc"""
    )
    out = []
    for r in rows:
        lv = await access.level_for(person, "skill", str(r["id"]))
        if lv == "none":
            continue
        out.append({**r, "level": lv})
    if query:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        def score(r: dict) -> int:
            hay = (r["name"] + " " + r["description"]).lower()
            return sum(hay.count(w) for w in words)
        out.sort(key=score, reverse=True)
        out = [r for r in out if score(r) > 0] or out
    return out[:limit]


async def get(person: access.Person, name: str, version: int | None = None) -> dict:
    s = await db.fetch_one("select * from skills where name = %s", (name,))
    if not s:
        raise ValueError(f"No skill called '{name}'. Use action 'find' to list skills.")
    lv = await access.level_for(person, "skill", str(s["id"]))
    if lv == "none":
        raise access.AccessDenied("That skill isn't shared with you.")
    v = await db.fetch_one(
        "select * from skill_versions where skill_id = %s and version = %s",
        (s["id"], version or s["current_version"]),
    )
    if not v:
        raise ValueError("No such version.")
    files: dict[str, Any] = {}
    for path, f in (v["files"] or {}).items():
        if "text" in f:
            files[path] = {"text": f["text"]} if len(f["text"]) <= INLINE_LIMIT else {
                "size": len(f["text"]), "note": "large; open it with skills action 'get' and file=<path>"}
        elif "file_key" in f:
            files[path] = {"size": f.get("size"), "download": storage.signed_url(f["file_key"], method="GET", filename=path.split("/")[-1])}
    return {"skill": s, "version": v, "files": files}


async def get_file(person: access.Person, name: str, path: str) -> dict:
    data = await get(person, name)
    f = (data["version"]["files"] or {}).get(path)
    if not f:
        raise ValueError(f"No file '{path}' in skill '{name}'.")
    if "text" in f:
        return {"path": path, "text": f["text"]}
    return {"path": path, "download": storage.signed_url(f["file_key"], method="GET", filename=path.split("/")[-1])}


async def save(person: access.Person, *, name: str, skill_md: str, files: dict[str, str] | None = None,
               description: str | None = None, owner: str | None = None, note: str = "") -> dict:
    """Create a skill, or add a new draft version of one you can edit."""
    if not NAME.match(name):
        raise ValueError("Skill names use lowercase letters, digits and hyphens (2–64 characters).")
    fm = _frontmatter(skill_md)
    desc = (description or fm.get("description") or "").strip()
    if not desc:
        raise ValueError("Give the skill a description (in the SKILL.md frontmatter or the description field).")
    stored_files = {p: {"text": t} for p, t in (files or {}).items()}
    async with db.tx() as conn:
        s = await db.fetch_one("select * from skills where name = %s for update", (name,), conn)
        if s is None:
            og, op = access.owner_fields(person, owner)
            s = await db.fetch_one(
                """insert into skills (name, description, status, owner_group, owner_person, current_version, created_by)
                   values (%s, %s, 'draft', %s, %s, 1, %s) returning *""",
                (name, desc, og, op, person.id),
                conn,
            )
            assert s is not None
            version = 1
        else:
            await access.require(person, "skill", str(s["id"]), "edit", conn)
            last = await db.fetch_one("select max(version) as v from skill_versions where skill_id=%s", (s["id"],), conn)
            version = (last["v"] or 0) + 1
            await conn.execute("update skills set description=%s, updated_at=now() where id=%s", (desc, s["id"]))
        await conn.execute(
            "insert into skill_versions (skill_id, version, skill_md, files, note, created_by) values (%s,%s,%s,%s,%s,%s)",
            (s["id"], version, skill_md, db.jsonb(stored_files), note, person.id),
        )
        if s["status"] == "draft":
            await conn.execute("update skills set current_version=%s where id=%s", (version, s["id"]))
    published = s["status"] == "published"
    return {
        "name": name, "version": version, "status": s["status"],
        "next": ("This is a new draft version; the published version stays live until a lead publishes this one "
                 f"(skills action 'publish', version {version}).") if published else
                "Saved as a draft. A lead of the owning team can publish it with skills action 'publish'.",
    }


async def publish(person: access.Person, name: str, version: int | None = None,
                  share_with: list[str] | None = None) -> dict:
    async with db.tx() as conn:
        s = await db.fetch_one("select * from skills where name=%s for update", (name,), conn)
        if not s:
            raise ValueError(f"No skill '{name}'.")
        lv = await access.require(person, "skill", str(s["id"]), "manage", conn)
        _ = lv
        v = version or (await db.fetch_one("select max(version) as v from skill_versions where skill_id=%s", (s["id"],), conn))["v"]
        ver = await db.fetch_one("select * from skill_versions where skill_id=%s and version=%s", (s["id"], v), conn)
        if not ver:
            raise ValueError("No such version.")
        await conn.execute(
            "update skills set status='published', current_version=%s, updated_at=now() where id=%s", (v, s["id"])
        )
        for who in share_with or []:
            if who == access.ALL_RL and not access.can_share_with_all_rl(person):
                raise access.AccessDenied("Only leads and admins can share with All RL.")
            await conn.execute(
                """insert into grants (resource_type, resource_id, principal_type, principal_id, level, granted_by)
                   values ('skill', %s, 'group', %s, 'view', %s) on conflict do nothing""",
                (s["id"], who, person.id),
            )
        await index.replace_chunks(conn, "skill", str(s["id"]), None, [_chunk(name, s["description"], ver["skill_md"])])
    return {"name": name, "status": "published", "version": v}


async def retire(person: access.Person, name: str) -> dict:
    async with db.tx() as conn:
        s = await db.fetch_one("select id from skills where name=%s", (name,), conn)
        if not s:
            raise ValueError(f"No skill '{name}'.")
        await access.require(person, "skill", str(s["id"]), "manage", conn)
        await conn.execute("update skills set status='retired', updated_at=now() where id=%s", (s["id"],))
        await index.drop_chunks(conn, "skill", str(s["id"]))
    return {"name": name, "status": "retired"}


async def feedback(person: access.Person, name: str, comment: str) -> dict:
    s = await db.fetch_one("select id from skills where name=%s", (name,))
    if not s:
        raise ValueError(f"No skill '{name}'.")
    await db.execute("insert into skill_feedback (skill_id, person_id, comment) values (%s,%s,%s)",
                     (s["id"], person.id, comment))
    return {"name": name, "feedback": "recorded"}


async def seed() -> list[str]:
    """Install or refresh the starter skills shipped with Nebula (published to everyone, owned by RL)."""
    from importlib import resources

    changed = []
    base = resources.files("nebula") / "seed_skills"
    for folder in sorted(p for p in base.iterdir() if p.is_dir()):
        md_file = folder / "SKILL.md"
        if not md_file.is_file():
            continue
        md = md_file.read_text()
        fm = _frontmatter(md)
        name, desc = fm.get("name") or folder.name, (fm.get("description") or "").strip()
        async with db.tx() as conn:
            s = await db.fetch_one("select * from skills where name=%s for update", (name,), conn)
            if s is None:
                s = await db.fetch_one(
                    """insert into skills (name, description, status, owner_group, current_version)
                       values (%s, %s, 'published', 'all_rl', 1) returning *""",
                    (name, desc), conn,
                )
                assert s is not None
                version = 1
            else:
                last = await db.fetch_one(
                    "select version, skill_md, note from skill_versions where skill_id=%s order by version desc limit 1",
                    (s["id"],), conn,
                )
                if last and (last["skill_md"] == md or last["note"] != "seed"):
                    continue  # unchanged, or edited by people since: leave it alone
                version = (last["version"] if last else 0) + 1
                await conn.execute("update skills set description=%s, current_version=%s, updated_at=now() where id=%s",
                                   (desc, version, s["id"]))
            await conn.execute(
                "insert into skill_versions (skill_id, version, skill_md, files, note) values (%s,%s,%s,'{}','seed')",
                (s["id"], version, md),
            )
            await index.replace_chunks(conn, "skill", str(s["id"]), None, [_chunk(name, desc, md)])
            changed.append(name)
    return changed
