"""Who someone is, and what they may see and change.

Rules, in one place:
- Accounts in the configured RL domains, and explicit All RL members, are in `all_rl`.
- Other signed-in accounts get no organisation-wide access.
- Admins can manage everything.
- A resource owned by a person: that person manages it.
- A resource owned by a group: the group's leads manage it, members can edit it.
- Grants (person or group, level view/edit/manage) add access on top.
- Items, pages, records and datasets inherit their collection's access; their own
  grants can add to it (sharing one page more widely than its notebook).
- Skills have no collection: owner + grants only, and a published skill shared
  with a group is visible to that group.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

from psycopg import AsyncConnection

from nebula import db
from nebula.config import settings

Level = Literal["none", "view", "edit", "manage"]
_RANK = {"none": 0, "view": 1, "edit": 2, "manage": 3}
ALL_RL = "all_rl"


def best(*levels: Level) -> Level:
    return max(levels, key=lambda lv: _RANK[lv]) if levels else "none"


def at_least(level: Level, needed: Level) -> bool:
    return _RANK[level] >= _RANK[needed]


class AccessDenied(Exception):
    pass


@dataclass
class Person:
    id: str
    email: str
    name: str
    is_admin: bool
    groups: dict[str, str] = field(default_factory=dict)  # group_id -> "member" | "lead"

    @property
    def lead_of(self) -> set[str]:
        return {g for g, r in self.groups.items() if r == "lead"}

    @property
    def is_lead(self) -> bool:
        return bool(self.lead_of)

    @property
    def is_rl_member(self) -> bool:
        return (self.is_admin or ALL_RL in self.groups
                or self.email.rsplit("@", 1)[-1].lower() in settings().allowed_domains)

    def principals(self) -> list[tuple[str, str]]:
        p = [("person", self.id)]
        p += [("group", g) for g in self.groups if g != ALL_RL]
        if self.is_rl_member:
            p.append(("group", ALL_RL))
        return p

    def group_level(self, group_id: str | None) -> Level:
        """Access a person has to something owned by `group_id`."""
        if not group_id:
            return "none"
        if group_id == ALL_RL:
            return "manage" if self.is_admin else "view" if self.is_rl_member else "none"
        role = self.groups.get(group_id)
        return "manage" if role == "lead" else "edit" if role == "member" else "none"


# Identity ------------------------------------------------------------------------


async def ensure_person(email: str, name: str = "", google_sub: str | None = None) -> Person:
    """Create or refresh the person behind a signed-in email."""
    email = email.strip().lower()
    admin = email in settings().admin_email_set
    async with db.tx() as conn:
        row = await db.fetch_one(
            """
            insert into people (email, name, google_sub, is_admin, last_seen_at)
            values (%s, %s, %s, %s, now())
            on conflict (email) do update set
                name = case when excluded.name <> '' then excluded.name else people.name end,
                google_sub = coalesce(people.google_sub, excluded.google_sub),
                is_admin = people.is_admin or excluded.is_admin,
                last_seen_at = now()
            returning id, email, name, is_admin, status
            """,
            (email, name or "", google_sub, admin),
            conn,
        )
        assert row is not None
        if row["status"] != "active":
            raise AccessDenied("This Nebula account is deactivated.")
        return await _load(conn, row)


async def get_person(email: str) -> Person | None:
    async with db.tx() as conn:
        row = await db.fetch_one(
            "select id, email, name, is_admin, status from people where email = %s",
            (email.strip().lower(),),
            conn,
        )
        if not row:
            return None
        return await _load(conn, row)


async def _load(conn: AsyncConnection, row: dict) -> Person:
    groups = await db.fetch_all(
        "select group_id, role from memberships where person_id = %s", (row["id"],), conn
    )
    return Person(
        id=str(row["id"]),
        email=row["email"],
        name=row["name"],
        is_admin=row["is_admin"],
        groups={g["group_id"]: g["role"] for g in groups},
    )


# Access checks ---------------------------------------------------------------------


def _principal_filter(person: Person) -> tuple[str, list]:
    """SQL fragment matching grants for any of the person's principals."""
    parts, params = [], []
    for ptype, pid in person.principals():
        parts.append("(principal_type = %s and principal_id = %s)")
        params += [ptype, pid]
    return "(" + " or ".join(parts) + ")", params


MIRRORED_KINDS = ("gdrive", "granola")


def mirrors_source(collection: dict) -> bool:
    """Drive files follow their own Drive sharing, and Granola notes are visible to the people who were
    in the meeting, unless the collection is set to permissions: "audience" (everything to its groups)."""
    return collection.get("kind") in MIRRORED_KINDS and (collection.get("source_config") or {}).get(
        "permissions", "mirror") == "mirror"


async def collection_levels(person: Person, conn: AsyncConnection | None = None, *,
                            content: bool = False) -> dict[str, Level]:
    """Every active collection the person can at least view, with their level.

    With content=True this is access to what's *inside*: for collections that mirror source
    sharing, owning the collection doesn't reveal its files; only explicit collection grants
    (a deliberate publish) and per-item grants mirrored from the source do.
    """
    cols = await db.fetch_all(
        "select id, kind, source_config, owner_group, owner_person from collections where status = 'active'",
        None, conn,
    )
    where, params = _principal_filter(person)
    grants = await db.fetch_all(
        f"select resource_id, level from grants where resource_type = 'collection' and {where}",
        params,
        conn,
    )
    granted: dict[str, Level] = {}
    for g in grants:
        rid = str(g["resource_id"])
        granted[rid] = best(granted.get(rid, "none"), g["level"])
    out: dict[str, Level] = {}
    for c in cols:
        cid = str(c["id"])
        lv: Level = "manage" if person.is_admin else "none"
        if not (content and mirrors_source(c)):
            if c["owner_person"] and str(c["owner_person"]) == person.id:
                lv = "manage"
            lv = best(lv, person.group_level(c["owner_group"]))
        lv = best(lv, granted.get(cid, "none"))
        if lv != "none":
            out[cid] = lv
    return out


async def resource_grants(person: Person, conn: AsyncConnection | None = None) -> dict[tuple[str, str], Level]:
    """Grants the person holds directly on single items/pages/records/datasets/skills."""
    where, params = _principal_filter(person)
    rows = await db.fetch_all(
        f"select resource_type, resource_id, level from grants where resource_type <> 'collection' and {where}",
        params,
        conn,
    )
    out: dict[tuple[str, str], Level] = {}
    for r in rows:
        key = (r["resource_type"], str(r["resource_id"]))
        out[key] = best(out.get(key, "none"), r["level"])
    return out


_TABLE = {"item": "items", "page": "pages", "record": "records", "dataset": "datasets", "skill": "skills"}


async def level_for(
    person: Person, rtype: str, rid: str, conn: AsyncConnection | None = None
) -> Level:
    """The person's access to one resource."""
    if person.is_admin:
        return "manage"
    if rtype == "collection":
        return (await collection_levels(person, conn)).get(rid, "none")
    table = _TABLE[rtype]
    has_owner = rtype in ("page", "record", "skill")
    cols = "owner_group, owner_person" if has_owner else "null as owner_group, null as owner_person"
    cols += ", collection_id" if rtype != "skill" else ", null as collection_id"
    if rtype == "skill":
        cols += ", status"
    row = await db.fetch_one(f"select {cols} from {table} where id = %s", (rid,), conn)
    if not row:
        return "none"
    lv: Level = "none"
    if row["owner_person"] and str(row["owner_person"]) == person.id:
        lv = "manage"
    lv = best(lv, person.group_level(row["owner_group"]))
    if row["collection_id"]:
        lv = best(lv, (await collection_levels(person, conn, content=True)).get(str(row["collection_id"]), "none"))
    where, params = _principal_filter(person)
    g = await db.fetch_one(
        f"""select level from grants where resource_type = %s and resource_id = %s and {where}
            order by case level when 'manage' then 3 when 'edit' then 2 else 1 end desc limit 1""",
        [rtype, rid, *params],
        conn,
    )
    if g:
        lv = best(lv, g["level"])
    if rtype == "skill" and row.get("status") == "draft" and not at_least(lv, "edit"):
        return "none"  # drafts are only visible to people who can work on them
    return lv


async def require(person: Person, rtype: str, rid: str, needed: Level, conn: AsyncConnection | None = None) -> Level:
    lv = await level_for(person, rtype, rid, conn)
    if not at_least(lv, needed):
        what = {"view": "see", "edit": "change", "manage": "manage sharing for"}[needed]
        raise AccessDenied(f"You don't have permission to {what} this {rtype}.")
    return lv


def can_share_with_all_rl(person: Person) -> bool:
    return person.is_admin or person.is_lead


async def visible_items(person: Person, collection_id: str, conn: AsyncConnection | None = None) -> set[str] | None:
    """None if the person can see everything in the collection, else the ids of the items they can.

    Managing a collection that mirrors its source (Drive, Granola) doesn't reveal what's in it: lists of
    titles must go through this, not through the collection's level.
    """
    if person.is_admin or collection_id in await collection_levels(person, conn, content=True):
        return None
    return {rid for (rtype, rid) in await resource_grants(person, conn) if rtype == "item"}


async def visible_scope(person: Person, conn: AsyncConnection | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """(collection ids the person can view, extra (type, id) pairs shared with them directly)."""
    cols = list((await collection_levels(person, conn, content=True)).keys())
    extras = [k for k in (await resource_grants(person, conn)).keys()]
    # Skills and pages owned by the person's groups or the person, outside visible collections
    return cols, extras


def owner_fields(person: Person, owner: str | None) -> tuple[str | None, str | None]:
    """Resolve an owner argument ("me", a group id, or None) to (owner_group, owner_person)."""
    if owner in (None, "", "me"):
        return None, person.id
    if owner == ALL_RL:
        if not person.is_admin:
            raise AccessDenied("Only admins can make RL the owner. Use a team instead.")
        return ALL_RL, None
    if owner not in person.groups and not person.is_admin:
        raise AccessDenied(f"You're not a member of '{owner}', so it can't own this.")
    return owner, None


def describe_level(lv: Level) -> str:
    return {"none": "no access", "view": "can view", "edit": "can edit", "manage": "can manage"}[lv]


def ids_of(rows: Iterable[dict], key: str = "id") -> list[str]:
    return [str(r[key]) for r in rows]
