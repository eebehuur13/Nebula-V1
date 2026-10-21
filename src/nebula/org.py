"""People, groups, sharing, collections and handover."""

from __future__ import annotations

import datetime as dt
import re

from psycopg import AsyncConnection

from nebula import access, db, jobs
from nebula.access import ALL_RL, AccessDenied, Person
from nebula.config import settings

LEVELS = ("view", "edit", "manage")


async def audit(conn: AsyncConnection | None, person: Person | None, action: str, rtype: str | None = None,
                rid: str | None = None, detail: dict | None = None) -> None:
    await db.execute(
        "insert into audit_log (person_id, action, resource_type, resource_id, detail) values (%s,%s,%s,%s,%s)",
        (person.id if person else None, action, rtype, rid, db.jsonb(detail or {})),
        conn,
    )


# Who's who ------------------------------------------------------------------------------


async def find_people(query: str = "", limit: int = 20, *, viewer: Person) -> list[dict]:
    q = f"%{query.strip().lower()}%"
    if viewer.is_admin:
        visible, visibility_params = "true", []
    elif viewer.is_rl_member:
        visible = """(split_part(lower(p.email), '@', 2) = any(%s) or p.is_admin
                     or exists (select 1 from memberships rl where rl.person_id=p.id and rl.group_id='all_rl'))"""
        visibility_params = [list(settings().allowed_domains)]
    else:
        visible, visibility_params = "p.id = %s", [viewer.id]
    return await db.fetch_all(
        f"""select p.email, p.name, p.status,
                  coalesce(json_agg(json_build_object('group', m.group_id, 'role', m.role))
                           filter (where m.group_id is not null), '[]') as groups
           from people p left join memberships m on m.person_id = p.id
           where (lower(p.email) like %s or lower(p.name) like %s) and {visible}
           group by p.id order by p.name limit %s""",
        [q, q, *visibility_params, limit],
    )


async def groups_overview() -> list[dict]:
    return await db.fetch_all(
        """select g.id, g.name, g.description,
                  coalesce(json_agg(json_build_object('email', p.email, 'name', p.name, 'role', m.role)
                           order by m.role desc, p.name) filter (where p.id is not null), '[]') as members
           from groups g left join memberships m on m.group_id = g.id left join people p on p.id = m.person_id
           group by g.id order by g.id"""
    )


async def person_overview(email: str) -> dict | None:
    p = await db.fetch_one("select id, email, name, status, is_admin from people where email=%s", (email.lower(),))
    if not p:
        return None
    groups = await db.fetch_all(
        "select m.group_id, g.name, m.role from memberships m join groups g on g.id=m.group_id where m.person_id=%s",
        (p["id"],),
    )
    roles = await db.fetch_all(
        """select r.name, a.starts_on, a.ends_on from appointments a join roles r on r.id = a.role_id
           where a.person_id = %s order by a.starts_on desc""",
        (p["id"],),
    )
    owns = await db.fetch_all(
        """select 'collection' as type, name as title from collections where owner_person=%(id)s
           union all select 'page', title from pages where owner_person=%(id)s
           union all select 'skill', name from skills where owner_person=%(id)s
           limit 50""",
        {"id": p["id"]},
    )
    return {"person": {k: p[k] for k in ("email", "name", "status", "is_admin")}, "groups": groups, "roles": roles,
            "owns": owns}


async def role_history(role_id: str) -> list[dict]:
    return await db.fetch_all(
        """select p.name, p.email, a.starts_on, a.ends_on from appointments a join people p on p.id = a.person_id
           where a.role_id = %s order by a.starts_on desc""",
        (role_id,),
    )


# Sharing ------------------------------------------------------------------------------


async def _principal(conn: AsyncConnection, who: str) -> tuple[str, str, str]:
    """'priya@…' -> person; 'ap' / 'all_rl' -> group. Returns (type, id, label)."""
    who = who.strip().lower()
    if "@" in who:
        p = await db.fetch_one("select id, name from people where email = %s", (who,), conn)
        if not p:
            raise ValueError(f"{who} hasn't connected Nebula yet, so they can't be added. They can sign in first.")
        return "person", str(p["id"]), p["name"] or who
    g = await db.fetch_one("select id, name from groups where id = %s", (who,), conn)
    if not g:
        raise ValueError(f"No group '{who}'. Use the people tool to list groups.")
    return "group", g["id"], g["name"]


async def access_list(conn: AsyncConnection | None, rtype: str, rid: str) -> dict:
    grants = await db.fetch_all(
        """select g.principal_type, g.principal_id, g.level, g.origin,
                  coalesce(p.email, gr.name) as label
           from grants g left join people p on g.principal_type='person' and p.id::text = g.principal_id
           left join groups gr on g.principal_type='group' and gr.id = g.principal_id
           where g.resource_type=%s and g.resource_id=%s order by g.level desc""",
        (rtype, rid),
        conn,
    )
    owner = None
    inherited = None
    if rtype in ("collection", "page", "record", "skill"):
        table = {"collection": "collections", "page": "pages", "record": "records", "skill": "skills"}[rtype]
        o = await db.fetch_one(
            f"""select t.owner_group, (select email from people where id = t.owner_person) as owner_email
                from {table} t where t.id=%s""",
            (rid,),
            conn,
        )
        if o:
            owner = o["owner_group"] and f"group {o['owner_group']} (leads manage, members edit)" or o["owner_email"]
    if rtype in ("item", "page", "record", "dataset"):
        table = {"item": "items", "page": "pages", "record": "records", "dataset": "datasets"}[rtype]
        c = await db.fetch_one(
            f"select c.id, c.name from {table} t join collections c on c.id=t.collection_id where t.id=%s", (rid,), conn
        )
        if c:
            inherited = {"collection": c["name"], "access": await access_list(conn, "collection", str(c["id"]))}
    return {
        "owner": owner,
        "shared_with": [{"who": g["label"], "type": g["principal_type"], "level": g["level"],
                         **({"from": "source sharing (updates on each sync)"} if g["origin"] == "source" else {})}
                        for g in grants],
        "also_from_collection": inherited,
    }


async def share(person: Person, rtype: str, rid: str, add: list[dict] | None, remove: list[str] | None) -> dict:
    async with db.tx() as conn:
        await access.require(person, rtype, rid, "manage", conn)
        changes = []
        for a in add or []:
            level = a.get("level", "view")
            if level not in LEVELS:
                raise ValueError("level must be view, edit or manage.")
            ptype, pid, label = await _principal(conn, a["who"])
            if pid == ALL_RL and not access.can_share_with_all_rl(person):
                raise AccessDenied("Only team leads and admins can share with All RL.")
            await conn.execute(
                """insert into grants (resource_type, resource_id, principal_type, principal_id, level, granted_by)
                   values (%s,%s,%s,%s,%s,%s)
                   on conflict (resource_type, resource_id, principal_type, principal_id)
                   do update set level=excluded.level, granted_by=excluded.granted_by, granted_at=now(),
                                 origin='nebula'""",
                (rtype, rid, ptype, pid, level, person.id),
            )
            changes.append(f"{label}: {level}")
        for who in remove or []:
            ptype, pid, label = await _principal(conn, who)
            await conn.execute(
                "delete from grants where resource_type=%s and resource_id=%s and principal_type=%s and principal_id=%s",
                (rtype, rid, ptype, pid),
            )
            changes.append(f"{label}: removed")
        if changes:
            await audit(conn, person, "share", rtype, rid, {"changes": changes})
        return {"changed": changes, "access": await access_list(conn, rtype, rid)}


# Groups and collections (admin) ------------------------------------------------------------


def _require_admin(person: Person) -> None:
    if not person.is_admin:
        raise AccessDenied("Only Nebula admins can do that.")


def _require_lead_of(person: Person, group_id: str) -> None:
    if not (person.is_admin or group_id in person.lead_of):
        raise AccessDenied(f"Only leads of '{group_id}' (or admins) can do that.")


async def create_group(person: Person, group_id: str, name: str, description: str = "",
                       google_group_email: str | None = None, lead_email: str | None = None) -> dict:
    _require_admin(person)
    if not re.match(r"^[a-z0-9_]{2,40}$", group_id):
        raise ValueError("Group ids are 2–40 lowercase letters, digits or underscores, e.g. 'ap' or 'comms'.")
    async with db.tx() as conn:
        await conn.execute(
            "insert into groups (id, name, description, google_group_email) values (%s,%s,%s,%s)",
            (group_id, name, description, google_group_email),
        )
        from nebula.knowledge.notes import ensure_group_notebook

        nb = await ensure_group_notebook(conn, group_id)
        if lead_email:
            await _set_member(conn, group_id, lead_email, "lead")
        await audit(conn, person, "create_group", "group", group_id)
    return {"group": group_id, "notebook": nb["slug"]}


async def set_group_email(person: Person, group_id: str, google_group_email: str | None) -> dict:
    """Link a Nebula team to a Google Group, so Drive files shared with that group are visible to the team."""
    from nebula.knowledge import items

    _require_admin(person)
    email = (google_group_email or "").strip().lower() or None
    async with db.tx() as conn:
        row = await db.fetch_one("update groups set google_group_email=%s where id=%s returning id", (email, group_id), conn)
        if not row:
            raise ValueError(f"No group '{group_id}'.")
        # Re-apply the sharing Nebula already holds for files shared with that Google Group (no Drive calls).
        affected = await db.fetch_all(
            """select i.id as item_id, i.source_audience, c.kind, c.source_config
               from items i join collections c on c.id = i.collection_id
               where i.status='active' and jsonb_exists(coalesce(i.source_audience->'groups', '[]'::jsonb), %s)""",
            (email or "",), conn) if email else []
        for r in affected:
            await items.apply_source_audience(conn, {"kind": r["kind"], "source_config": r["source_config"]},
                                              str(r["item_id"]), r["source_audience"])
        await audit(conn, person, "set_group_email", "group", group_id, {"google_group_email": email})
    return {"group": group_id, "google_group_email": email, "files_now_visible_to_the_team": len(affected)}


async def _set_member(conn: AsyncConnection, group_id: str, email: str, role: str) -> None:
    p = await db.fetch_one("select id from people where email=%s", (email.lower(),), conn)
    if not p:
        p = await db.fetch_one("insert into people (email) values (%s) returning id", (email.lower(),), conn)
    assert p is not None
    await conn.execute(
        """insert into memberships (group_id, person_id, role) values (%s,%s,%s)
           on conflict (group_id, person_id) do update set role = excluded.role""",
        (group_id, p["id"], role),
    )


async def set_member(person: Person, group_id: str, email: str, role: str = "member") -> dict:
    if role not in ("member", "lead"):
        raise ValueError("role must be member or lead.")
    _require_lead_of(person, group_id)
    async with db.tx() as conn:
        await _set_member(conn, group_id, email, role)
        await audit(conn, person, "set_member", "group", group_id, {"email": email, "role": role})
    return {"group": group_id, "email": email, "role": role}


async def remove_member(person: Person, group_id: str, email: str) -> dict:
    _require_lead_of(person, group_id)
    async with db.tx() as conn:
        await conn.execute(
            "delete from memberships where group_id=%s and person_id=(select id from people where email=%s)",
            (group_id, email.lower()),
        )
        await audit(conn, person, "remove_member", "group", group_id, {"email": email})
    return {"group": group_id, "removed": email}


async def create_collection(person: Person, *, slug: str, name: str, kind: str, source_config: dict,
                            owner_group: str, audience: list[str] | None = None, description: str = "") -> dict:
    """Register a source root (or a notebook). Audience = groups that can view it, besides the owner group."""
    _require_lead_of(person, owner_group)
    if kind not in ("notebook", "gdrive", "notion", "granola"):
        raise ValueError("kind must be gdrive, notion, granola or notebook.")
    async with db.tx() as conn:
        c = await db.fetch_one(
            """insert into collections (slug, name, description, kind, source_config, owner_group, created_by)
               values (%s,%s,%s,%s,%s,%s,%s) returning *""",
            (slug, name, description, kind, db.jsonb(source_config), owner_group, person.id),
            conn,
        )
        assert c is not None
        for g in audience or []:
            if g == ALL_RL and not access.can_share_with_all_rl(person):
                raise AccessDenied("Only leads and admins can open a collection to All RL.")
            await conn.execute(
                """insert into grants (resource_type, resource_id, principal_type, principal_id, level, granted_by)
                   values ('collection', %s, 'group', %s, 'view', %s) on conflict do nothing""",
                (c["id"], g, person.id),
            )
        job_id = None
        if kind != "notebook":
            job_id = await jobs.enqueue("sync", {"collection_id": str(c["id"]), "full": True}, conn=conn,
                                        dedupe_key=f"sync:{c['id']}", person_id=person.id)
        await audit(conn, person, "create_collection", "collection", str(c["id"]), {"kind": kind, "audience": audience})
    return {"collection": slug, "id": str(c["id"]), "first_sync_job": job_id, "who_can_see": who_can_see(c)}


def who_can_see(c: dict) -> str:
    """How access works for a collection, in a sentence Claude can pass on."""
    if c["kind"] == "notebook":
        return "The owning team (members edit, leads manage), plus anyone it's shared with."
    published = "; the groups in its audience see everything in it"
    if access.mirrors_source(c):
        if c["kind"] == "gdrive":
            return "Each file is visible to the people Drive shares it with" + published + "."
        return ("Each meeting note is visible to the people who were in the meeting (its owner, attendees and "
                "invitees)" + published + ".")
    if c["kind"] == "notion":
        return ("Everyone in the owning team and its audience sees every page under the root. Notion's own page "
                "permissions don't carry over: leave restricted pages out with exclude_page_ids, and use Claude's "
                "own Notion connector for them.")
    return "Everyone in the owning team and its audience sees everything in it."


async def list_collections(person: Person) -> list[dict]:
    levels = await access.collection_levels(person)
    rows = await db.fetch_all(
        """select c.id, c.slug, c.name, c.kind, c.owner_group, c.status, c.last_synced_at, c.sync_error,
                  (select count(*) from items i where i.collection_id=c.id and i.status='active') as items,
                  (select count(*) from pages p where p.collection_id=c.id) as pages,
                  (select count(*) from records r where r.collection_id=c.id) as records
           from collections c where c.status <> 'archived' order by c.name"""
    )
    return [{**r, "your_access": levels[str(r["id"])]} for r in rows if str(r["id"]) in levels]


async def sync_now(person: Person, collection: str, full: bool = False) -> dict:
    c = await db.fetch_one("select id, kind, owner_group from collections where slug=%s or id::text=%s",
                           (collection, collection))
    if not c:
        raise ValueError(f"No collection '{collection}'.")
    await access.require(person, "collection", str(c["id"]), "manage")
    job = await jobs.enqueue("sync", {"collection_id": str(c["id"]), "full": full}, dedupe_key=f"sync:{c['id']}",
                             person_id=person.id, priority=20)
    return {"job": job}


# Review queue ----------------------------------------------------------------------------


async def review_queue(person: Person) -> dict:
    levels = await access.collection_levels(person)
    managed = [cid for cid, lv in levels.items() if lv == "manage"]
    stale = await db.fetch_all(
        """select id, title, review_by, status from pages
           where collection_id = any(%s::uuid[]) and review_by < current_date and status <> 'archived'
           order by review_by limit 30""",
        (managed,),
    )
    proposals = await db.fetch_all(
        """select pr.id, p.title, pr.note, pe.email as proposed_by, pr.created_at
           from proposals pr join pages p on p.id = pr.page_id left join people pe on pe.id = pr.proposed_by
           where pr.status = 'open' and p.collection_id = any(%s::uuid[]) order by pr.created_at limit 30""",
        (managed,),
    )
    drafts = await db.fetch_all(
        """select id, type, title, created_at from records
           where collection_id = any(%s::uuid[]) and status = 'draft' order by created_at desc limit 30""",
        (managed,),
    )
    skills_ = await db.fetch_all(
        """select name, description, updated_at from skills where status='draft'
           and (owner_group = any(%s::text[]) or %s) order by updated_at desc limit 20""",
        (list(person.lead_of), person.is_admin),
    )
    return {
        "stale_pages": [{"id": f"page:{r['id']}", "title": r["title"], "review_was_due": str(r["review_by"])} for r in stale],
        "open_proposals": [{"proposal_id": str(r["id"]), "page": r["title"], "note": r["note"],
                            "by": r["proposed_by"]} for r in proposals],
        "draft_records": [{"id": f"record:{r['id']}", "type": r["type"], "title": r["title"]} for r in drafts],
        "draft_skills": [{"name": r["name"], "description": r["description"]} for r in skills_],
    }


# Handover ---------------------------------------------------------------------------------


async def handover(person: Person, *, from_email: str, to_email: str, group: str | None = None,
                   role_id: str | None = None, leaving: bool = False, apply: bool = False,
                   include_personal: bool = False) -> dict:
    """Preview (apply=False) or carry out a handover from one person to another.

    An admin can hand over anything. A team lead's handover stays inside their team: the person must
    be in it, only work in the team's own collections moves, and only an admin can deactivate someone.
    """
    if not (person.is_admin or (group and group in person.lead_of)):
        raise AccessDenied("Handover needs an admin, or a lead of the group being handed over.")
    team_only = not person.is_admin
    if team_only and leaving:
        raise AccessDenied("Only an admin can hand over someone who's leaving RL, because it deactivates their account.")
    async with db.tx() as conn:
        src = await db.fetch_one("select * from people where email=%s", (from_email.lower(),), conn)
        dst = await db.fetch_one("select * from people where email=%s", (to_email.lower(),), conn)
        if not src:
            raise ValueError(f"No Nebula account for {from_email}.")
        if team_only and not await db.fetch_one(
                "select 1 from memberships where group_id=%s and person_id=%s", (group, src["id"]), conn):
            raise AccessDenied(f"{from_email} isn't in '{group}', so a lead of '{group}' can't hand over their work.")
        if not dst:
            dst = await db.fetch_one("insert into people (email) values (%s) returning *", (to_email.lower(),), conn)
        assert dst is not None
        sid, did = src["id"], dst["id"]
        memberships = await db.fetch_all("select group_id, role from memberships where person_id=%s", (sid,), conn)
        lead_groups = [m["group_id"] for m in memberships if m["role"] == "lead" and (not group or m["group_id"] == group)]
        excl = "" if include_personal else " and {c} not in (select id from collections where slug like 'me-%%')"
        if team_only:  # only the team's own work: pages and records in collections the team owns
            excl = " and {c} in (select id from collections where owner_group = %s)"
        args = (sid, group) if team_only else (sid,)
        owned = {
            "collection": [] if team_only else await db.fetch_all(
                "select id, name as title from collections where owner_person=%s"
                + ("" if include_personal else " and slug not like 'me-%%'"), (sid,), conn),
            "page": await db.fetch_all("select id, title from pages where owner_person=%s" + excl.format(c="collection_id"),
                                       args, conn),
            "record": await db.fetch_all("select id, title from records where owner_person=%s" + excl.format(c="collection_id"),
                                         args, conn),
            "skill": [] if team_only else await db.fetch_all(
                "select id, name as title from skills where owner_person=%s", (sid,), conn),
        }
        roles = await db.fetch_all(
            """select a.id, r.id as role_id, r.name from appointments a join roles r on r.id=a.role_id
               where a.person_id=%s and a.ends_on is null and (%s::text is null or r.id=%s)""",
            (sid, role_id, role_id),
            conn,
        )
        open_proposals = await db.fetch_all(
            "select count(*) as n from proposals where proposed_by=%s and status='open'", (sid,), conn
        )
        preview = {
            "from": from_email, "to": to_email,
            "lead_of": lead_groups,
            "roles": [r["name"] for r in roles],
            "personally_owned": {t: [r["title"] for r in rows] for t, rows in owned.items() if rows},
            "open_proposals_by_them": open_proposals[0]["n"] if open_proposals else 0,
            "leaving_rl": leaving,
            "still_to_do_by_hand": [
                "Recreate any Claude scheduled tasks they ran for the team (they live in their own Claude account).",
                "Move any of their My Drive files the team relies on into a Shared Drive.",
                "Check the source systems (Drive, Notion, Granola) for memberships Nebula can't change.",
            ],
        }
        if not apply:
            return {"preview": preview, "next": "Call again with apply=true to carry this out."}

        today = dt.date.today()
        for g in lead_groups:
            await conn.execute(
                """insert into memberships (group_id, person_id, role) values (%s,%s,'lead')
                   on conflict (group_id, person_id) do update set role='lead'""",
                (g, did),
            )
            await conn.execute(
                "update memberships set role='member' where group_id=%s and person_id=%s", (g, sid)
            )
        for r in roles:
            await conn.execute("update appointments set ends_on=%s where id=%s", (today, r["id"]))
            await conn.execute(
                "insert into appointments (role_id, person_id, starts_on, created_by) values (%s,%s,%s,%s)",
                (r["role_id"], did, today, person.id),
            )
        # Work they own personally moves to the successor (exactly what the preview listed); their
        # private notebook stays theirs unless include_personal is set.
        for tbl, rows in (("collections", owned["collection"]), ("pages", owned["page"]),
                          ("records", owned["record"]), ("skills", owned["skill"])):
            if rows:
                await conn.execute(f"update {tbl} set owner_person=%s where id = any(%s::uuid[])",
                                   (did, [r["id"] for r in rows]))
        # Things shared with the predecessor personally are not passed on: access follows the
        # role (group leadership) and ownership, never someone's private shares.
        if leaving:
            await conn.execute("delete from memberships where person_id=%s", (sid,))
            await conn.execute("delete from grants where principal_type='person' and principal_id=%s", (str(sid),))
            await conn.execute("update people set status='deactivated' where id=%s", (sid,))
        elif group:
            await conn.execute("delete from memberships where person_id=%s and group_id=%s and role='member'",
                               (sid, group))
        await audit(conn, person, "handover", "person", str(sid), {"to": to_email, "preview": preview})
    return {"done": True, "summary": preview}


# Upkeep and export ---------------------------------------------------------------------------


async def recent_changes(person: Person, days: int = 7, collection: str | None = None) -> dict:
    """Sources, pages and records added or changed recently in collections the person can see."""
    levels = await access.collection_levels(person)
    cols = list(levels)
    if collection:
        c = await db.fetch_one("select id from collections where slug=%s or id::text=%s", (collection, collection))
        cols = [str(c["id"])] if c and str(c["id"]) in levels else []
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=max(1, min(days, 90)))
    content = await access.collection_levels(person, content=True)
    granted = {rid for (t, rid) in await access.resource_grants(person) if t == "item"}
    items_ = await db.fetch_all(
        """select i.id, i.title, i.kind, i.status, i.updated_at, i.collection_id, c.name as collection from items i
           join collections c on c.id = i.collection_id
           where (i.collection_id = any(%s::uuid[]) or (%s and i.id = any(%s::uuid[])))
             and i.updated_at >= %s order by i.updated_at desc limit 200""",
        (cols, not collection, list(granted), since),
    )
    # Managing a collection that mirrors its source doesn't reveal its files: list only what they can open.
    items_ = [r for r in items_ if person.is_admin or str(r["collection_id"]) in content or str(r["id"]) in granted][:60]
    pages = await db.fetch_all(
        """select id, title, status, version, updated_at from pages
           where collection_id = any(%s::uuid[]) and updated_at >= %s order by updated_at desc limit 40""",
        (cols, since),
    )
    records = await db.fetch_all(
        """select id, type, title, status, updated_at from records
           where collection_id = any(%s::uuid[]) and updated_at >= %s order by updated_at desc limit 40""",
        (cols, since),
    )
    return {
        "since": since.date().isoformat(),
        "sources": [{"id": f"item:{r['id']}", "title": r["title"], "kind": r["kind"], "status": r["status"],
                     "collection": r["collection"], "changed": r["updated_at"]} for r in items_],
        "pages": [{"id": f"page:{r['id']}", "title": r["title"], "status": r["status"], "version": r["version"],
                   "changed": r["updated_at"]} for r in pages],
        "records": [{"id": f"record:{r['id']}", "type": r["type"], "title": r["title"], "status": r["status"],
                     "changed": r["updated_at"]} for r in records],
    }


def _filename(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", title).strip()[:100] or "untitled"


async def export_collection(person: Person, collection: str) -> dict:
    """An Obsidian-ready zip of a collection's pages and records ([[links]] resolve by title)."""
    import io
    import zipfile

    from nebula import storage

    c = await db.fetch_one("select * from collections where slug=%s or id::text=%s", (collection, collection))
    if not c:
        raise ValueError(f"No collection '{collection}'.")
    await access.require(person, "collection", str(c["id"]), "view")
    pages = await db.fetch_all("select * from pages where collection_id=%s and status <> 'archived'", (c["id"],))
    records = await db.fetch_all("select * from records where collection_id=%s", (c["id"],))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        index = [f"# {c['name']}\n", f"Exported from Nebula on {dt.date.today():%d %B %Y}.\n", "## Pages"]
        for p in pages:
            fm = (f"---\nnebula_id: page:{p['id']}\nkind: {p['kind']}\nstatus: {p['status']}\n"
                  f"aliases: {list(p['aliases'] or [])}\nversion: {p['version']}\n"
                  f"updated: {p['updated_at']:%Y-%m-%d}\n---\n\n")
            z.writestr(f"{_filename(p['title'])}.md", fm + f"# {p['title']}\n\n{p['body']}\n")
            index.append(f"- [[{_filename(p['title'])}]] ({p['status']})")
        index.append("\n## Records")
        for r in records:
            fm = (f"---\nnebula_id: record:{r['id']}\ntype: {r['type']}\nstatus: {r['status']}\n"
                  f"valid_from: {r['valid_from'] or ''}\nvalid_to: {r['valid_to'] or ''}\n---\n\n")
            name = f"records/{r['type']} - {_filename(r['title'])}.md"
            z.writestr(name, fm + f"# {r['title']}\n\n{r['body']}\n")
            index.append(f"- [[{name.removesuffix('.md')}]] ({r['type']}, {r['status']})")
        z.writestr("index.md", "\n".join(index) + "\n")
    key = f"exports/collection/{c['id']}/{dt.datetime.now(dt.UTC):%Y%m%d%H%M%S}.zip"
    await storage.put(key, buf.getvalue(), "application/zip")
    name = f"{c['slug']}-obsidian.zip"
    url = storage.signed_url(key, method="GET", filename=name, mime="application/zip", ttl=900)
    return {"filename": name, "pages": len(pages), "records": len(records), "url": url,
            "how": f"curl -fsSL -o '{name}' '{url}'  (unzip it into an Obsidian vault)"}


# Sources people connect themselves --------------------------------------------------------------


async def connect_drive(person: Person, folder: str, name: str | None = None) -> dict:
    """Connect a Drive folder, file or Shared Drive that's shared with Nebula's service account.

    It becomes a root of the one Drive source; each file stays visible only to the people Drive
    shares it with (see `nebula.sources`).
    """
    from nebula import sources

    return await sources.connect(person, folder, name)


async def source_status(person: Person, collection: str) -> dict:
    c = await db.fetch_one("select * from collections where slug=%s or id::text=%s", (collection, collection))
    if not c:
        raise ValueError(f"No collection '{collection}'.")
    await access.require(person, "collection", str(c["id"]), "view")
    counts = await db.fetch_one(
        """select count(*) filter (where status='active') as active,
                  count(*) filter (where status='deleted') as deleted,
                  count(*) filter (where status_note <> '') as with_notes from items where collection_id=%s""",
        (c["id"],),
    )
    pending = await db.fetch_one(
        "select count(*) as n from jobs where kind='sync' and status in ('queued','running') "
        "and payload->>'collection_id'=%s", (str(c["id"]),),
    )
    unindexed = await db.fetch_one(
        "select count(*) as n from chunks where collection_id=%s and embedding is null", (c["id"],))
    recent = await db.fetch_all(
        "select id, title, kind, updated_at, source_audience from items where collection_id=%s and status='active' "
        "order by updated_at desc limit 10", (c["id"],),
    )
    recent = [r for r in recent if access.at_least(await access.level_for(person, "item", str(r["id"])), "view")]
    return {
        "collection": c["slug"], "name": c["name"], "kind": c["kind"], "status": c["status"],
        "last_synced_at": c["last_synced_at"], "sync_error": c["sync_error"],
        "sync_running_or_queued": bool(pending and pending["n"]),
        "files": counts, "pieces_waiting_for_search_index": unindexed["n"] if unindexed else 0,
        "recent": [{"id": f"item:{r['id']}", "title": r["title"], "kind": r["kind"], "updated": r["updated_at"],
                    "shared_with": (r["source_audience"] or {}).get("emails", [])} for r in recent],
    }


async def disconnect(person: Person, collection: str) -> dict:
    c = await db.fetch_one("select id, slug from collections where slug=%s or id::text=%s", (collection, collection))
    if not c:
        raise ValueError(f"No collection '{collection}'.")
    await access.require(person, "collection", str(c["id"]), "manage")
    async with db.tx() as conn:
        await conn.execute("update collections set status='archived' where id=%s", (c["id"],))
        await conn.execute("delete from chunks where collection_id=%s", (c["id"],))
        await conn.execute(
            "delete from grants where origin='source' and ((resource_type='item' and resource_id in "
            "(select id from items where collection_id=%s)) or (resource_type='dataset' and resource_id in "
            "(select id from datasets where collection_id=%s)))", (c["id"], c["id"]))
        await conn.execute("update items set status='deleted', updated_at=now() where collection_id=%s", (c["id"],))
        from nebula.knowledge import items

        ids = await db.fetch_all("select id from items where collection_id=%s", (c["id"],), conn)
        await items.purge_files(conn, [str(r["id"]) for r in ids])
        await audit(conn, person, "disconnect", "collection", str(c["id"]))
    return {"collection": c["slug"], "status": "disconnected",
            "note": "Its files are no longer searchable. The originals in Drive are untouched."}
