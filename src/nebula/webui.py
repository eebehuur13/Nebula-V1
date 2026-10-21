"""The Nebula web pages: browse what's in Nebula, Obsidian-style.

People mostly use Nebula through Claude. These pages are for looking around: a collection's
pages and records, a page with its links in and out, the graph of a collection, and the
review queue. Google sign-in is kept in a signed cookie. Every page checks access with
the same rules as the MCP tools.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from urllib.parse import quote, urlencode

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from nebula import access, db, org
from nebula.config import settings
from nebula.knowledge import items, links, render
from nebula.server import mcp

COOKIE = "nebula_session"
STATE_COOKIE = "nebula_login_state"
SESSION_DAYS = 14


# Sessions -------------------------------------------------------------------------------------


def _sign(data: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(settings().jwt_signing_key.encode(), ("web:" + raw).encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{mac}"


def _unsign(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    raw, mac = token.rsplit(".", 1)
    good = hmac.new(settings().jwt_signing_key.encode(), ("web:" + raw).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, good):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return data if isinstance(data, dict) and float(data.get("exp", 0)) > time.time() else None
    except (ValueError, TypeError):
        return None


async def viewer(request: Request) -> access.Person | None:
    s = settings()
    if s.auth_mode == "dev":
        return await access.ensure_person(s.dev_user_email, "Dev User")
    sess = _unsign(request.cookies.get(COOKIE))
    if not sess:
        return None
    if not s.email_allowed(sess.get("email", "")):
        return None
    try:
        return await access.ensure_person(sess["email"], sess.get("name", ""))
    except access.AccessDenied:
        return None


def _login_redirect(request: Request) -> Response:
    return RedirectResponse("/login?next=" + quote(request.url.path), status_code=302)


@mcp.custom_route("/login", methods=["GET"], include_in_schema=False)
async def login(request: Request) -> Response:
    s = settings()
    nxt = request.query_params.get("next", "/web")
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or any(ord(c) < 32 for c in nxt):
        nxt = "/web"
    state = _sign({"next": nxt, "n": secrets.token_urlsafe(8), "exp": time.time() + 600})
    params = {
        "client_id": s.google_client_id, "redirect_uri": f"{s.public_url}/web/callback", "response_type": "code",
        "scope": "openid email profile", "state": state, "prompt": "select_account",
    }
    if len(s.allowed_domains) == 1 and not s.allowed_email_set and not s.public_signup:
        params["hd"] = next(iter(s.allowed_domains))
    resp = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params), status_code=302)
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True,
                    secure=s.public_url.startswith("https"), samesite="lax", path="/web/callback")
    return resp


@mcp.custom_route("/web/callback", methods=["GET"], include_in_schema=False)
async def callback(request: Request) -> Response:
    s = settings()
    raw_state = request.query_params.get("state", "")
    cookie_state = request.cookies.get(STATE_COOKIE, "")
    state = _unsign(raw_state) if cookie_state and hmac.compare_digest(raw_state, cookie_state) else None
    code = request.query_params.get("code")
    if not state or not code:
        return _page("Sign-in failed", "<p>That sign-in link expired. <a href='/login'>Try again</a>.</p>")
    async with httpx.AsyncClient(timeout=10) as client:
        tok = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": s.google_client_id, "client_secret": s.google_client_secret,
            "redirect_uri": f"{s.public_url}/web/callback", "grant_type": "authorization_code",
        })
        if tok.status_code != 200:
            return _page("Sign-in failed", "<p>Google didn't accept the sign-in. <a href='/login'>Try again</a>.</p>")
        info = await client.get("https://openidconnect.googleapis.com/v1/userinfo",
                                headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
    user = info.json()
    email = (user.get("email") or "").lower()
    if not user.get("email_verified") or not s.email_allowed(email):
        return _page("Not allowed", "<p>This account cannot sign in to Nebula. Contact the administrator.</p>")
    try:
        await access.ensure_person(email, user.get("name", ""), user.get("sub"))
    except access.AccessDenied:
        return _page("Not allowed", "<p>This Nebula account is deactivated. Contact the administrator.</p>")
    resp = RedirectResponse(state["next"], status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/web/callback")
    resp.set_cookie(COOKIE, _sign({"email": email, "name": user.get("name", ""), "exp": time.time() + SESSION_DAYS * 86400}),
                    max_age=SESSION_DAYS * 86400, httponly=True, secure=s.public_url.startswith("https"), samesite="lax")
    return resp


@mcp.custom_route("/logout", methods=["GET"], include_in_schema=False)
async def logout(request: Request) -> Response:
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp


# Layout ---------------------------------------------------------------------------------------

_HEAD = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{title} · Nebula</title>
<style>
:root{--bg:#fbfaf7;--panel:#ffffff;--fg:#1d1d1b;--muted:#6b6a64;--line:#e7e4dc;--accent:#3d5afe;--chip:#eef0ff;
--ok:#1b7f4b;--warn:#a15c00}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1c1c1a;--fg:#ecebe6;--muted:#a3a29b;--line:#2c2b28;
--accent:#8c9eff;--chip:#23263a;--ok:#5ccf91;--warn:#e0a54a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,-apple-system,sans-serif}
header{border-bottom:1px solid var(--line);background:var(--panel)}
.bar{max-width:980px;margin:0 auto;padding:12px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.bar a{color:var(--fg);text-decoration:none}.bar .brand{font-weight:700}.bar .sp{flex:1}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.7rem;line-height:1.25;margin:0 0 6px}h2{font-size:1.15rem;margin:28px 0 8px}
a{color:var(--accent)}.meta{color:var(--muted);font-size:.92rem}
.chip{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--chip);font-size:.8rem;margin-right:4px}
.verified{color:var(--ok)}.draft,.stale{color:var(--warn)}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:32px}
@media (max-width:760px){.grid{grid-template-columns:1fr}}
aside{border-left:1px solid var(--line);padding-left:20px}@media (max-width:760px){aside{border:0;padding:0}}
ul.plain{list-style:none;padding:0;margin:0}ul.plain li{padding:6px 0;border-bottom:1px solid var(--line)}
.body{overflow-wrap:anywhere}.body table{border-collapse:collapse;display:block;overflow-x:auto}
.body td,.body th{border:1px solid var(--line);padding:4px 8px}.body pre{overflow-x:auto;background:var(--panel);padding:12px}
#graph{width:100%;height:70vh;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
</style>
<header><div class=bar><a class=brand href="/web">Nebula</a><a href="/web">Collections</a><a href="/web/review">Review</a>
<span class=sp></span>{who}</div></header><main>"""


def _page(title: str, body: str, person: access.Person | None = None, scripts: str = "") -> HTMLResponse:
    who = (f"<span class=meta>{html.escape(person.email)}</span> <a href='/logout'>Sign out</a>" if person
           else "<a href='/login'>Sign in</a>")
    head = _HEAD.replace("{title}", html.escape(title)).replace("{who}", who)
    from nebula.config import settings

    links = settings().footer_links()
    footer = f"<p class=meta>{links}</p>" if links else ""
    return HTMLResponse(head + body + footer + "</main>" + scripts + "</html>")


def _status(status: str, stale: bool = False) -> str:
    s = f"<span class='chip {html.escape(status)}'>{html.escape(status)}</span>"
    return s + ("<span class='chip stale'>review due</span>" if stale else "")


def _ref_link(t: str, i: str, title: str | None) -> str:
    href = {"page": f"/page/{i}", "record": f"/record/{i}", "item": f"/item/{i}"}.get(t, "#")
    return f"<a href='{href}'>{html.escape(title or t)}</a> <span class=meta>{t}</span>"


_MD_SCRIPTS = """<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<script>
document.querySelectorAll('[data-md]').forEach(el=>{
  let md=el.textContent;
  md=md.replace(/\\[\\[([^\\]|#]+)(?:#[^\\]|]*)?(?:\\|([^\\]]*))?\\]\\]/g,(m,t,a)=>`[${a||t}](/web/go?title=${encodeURIComponent(t)})`);
  md=md.replace(/nebula:(item|page|record|dataset):([0-9a-f-]{36})(#[\\w=.,:-]+)?/g,(m,k,i)=>`[↗ ${k}](/${k}/${i})`);
  el.innerHTML=DOMPurify.sanitize(marked.parse(md));el.hidden=false;});
</script>"""


# Pages ----------------------------------------------------------------------------------------


@mcp.custom_route("/web", methods=["GET"], include_in_schema=False)
async def web_home(request: Request) -> Response:
    person = await viewer(request)
    if not person:
        return _login_redirect(request)
    cols = await org.list_collections(person)
    rows = "".join(
        f"<li><a href='/web/c/{html.escape(c['slug'])}'>{html.escape(c['name'])}</a> "
        f"<span class=meta>{c['kind']} · {c['pages']} pages · {c['records']} records · {c['items']} sources · "
        f"you {c['your_access']}</span></li>" for c in cols
    ) or ("<li class=meta>Nothing here yet. In Claude, save a note or page to start your notebook, or ask "
          "Claude to connect a Google Drive folder (the sources tool).</li>")
    shared = await db.fetch_all(
        """select distinct on (g.resource_type, g.resource_id) g.resource_type, g.resource_id, g.origin,
                  coalesce(i.title, p.title, r.title) as title
           from grants g
           left join items i on g.resource_type='item' and i.id=g.resource_id and i.status='active'
           left join pages p on g.resource_type='page' and p.id=g.resource_id
           left join records r on g.resource_type='record' and r.id=g.resource_id
           where g.resource_type in ('item','page','record') and g.principal_type='person' and g.principal_id=%s
             and coalesce(i.title, p.title, r.title) is not null
           order by g.resource_type, g.resource_id limit 100""",
        (person.id,),
    )
    sl = "".join(
        f"<li>{_ref_link(s['resource_type'], str(s['resource_id']), s['title'])}"
        f"{' <span class=meta>· shared in Drive</span>' if s['origin'] == 'source' else ''}</li>" for s in shared
    )
    return _page("Collections", f"<h1>Collections</h1><p class=meta>Everything in Nebula you can see.</p>"
                                f"<ul class=plain>{rows}</ul>"
                                + (f"<h2>Shared with you</h2><ul class=plain>{sl}</ul>" if sl else ""), person)


@mcp.custom_route("/web/c/{slug}", methods=["GET"], include_in_schema=False)
async def web_collection(request: Request) -> Response:
    person = await viewer(request)
    if not person:
        return _login_redirect(request)
    c = await db.fetch_one("select * from collections where slug=%s", (request.path_params["slug"],))
    if not c or not access.at_least(await access.level_for(person, "collection", str(c["id"])), "view"):
        return _page("Not found", "<h1>Not found</h1>", person)
    pages = await db.fetch_all(
        "select id, title, kind, status, review_by from pages where collection_id=%s and status<>'archived' order by title",
        (c["id"],))
    recs = await db.fetch_all(
        "select id, type, title, status from records where collection_id=%s order by updated_at desc limit 200", (c["id"],))
    allowed = await access.visible_items(person, str(c["id"]))
    srcs = await db.fetch_all(
        """select id, title, kind from items where collection_id=%s and status='active'
           and (%s::uuid[] is null or id = any(%s::uuid[])) order by updated_at desc limit 100""",
        (c["id"], None if allowed is None else list(allowed), None if allowed is None else list(allowed)))
    import datetime as dt

    today = dt.date.today()
    pl = "".join(f"<li><a href='/page/{p['id']}'>{html.escape(p['title'])}</a> <span class=meta>{p['kind']}</span> "
                 f"{_status(p['status'], bool(p['review_by'] and p['review_by'] < today))}</li>" for p in pages)
    rl = "".join(f"<li><a href='/record/{r['id']}'>{html.escape(r['title'])}</a> <span class=meta>{r['type']}</span> "
                 f"{_status(r['status'])}</li>" for r in recs)
    sl = "".join(f"<li><a href='/item/{s['id']}'>{html.escape(s['title'])}</a> <span class=meta>{s['kind']}</span></li>"
                 for s in srcs)
    body = (f"<h1>{html.escape(c['name'])}</h1><p class=meta>{html.escape(c['description'])} · "
            f"<a href='/web/c/{c['slug']}/graph'>Graph view</a></p>"
            f"<h2>Pages</h2><ul class=plain>{pl or '<li class=meta>None yet</li>'}</ul>"
            f"<h2>Records</h2><ul class=plain>{rl or '<li class=meta>None yet</li>'}</ul>"
            f"<h2>Sources</h2><ul class=plain>{sl or '<li class=meta>None</li>'}</ul>")
    return _page(c["name"], body, person)


@mcp.custom_route("/web/go", methods=["GET"], include_in_schema=False)
async def web_go(request: Request) -> Response:
    """Follow a [[Page title]] link."""
    person = await viewer(request)
    if not person:
        return _login_redirect(request)
    async with db.tx() as conn:
        pid = await links.resolve_page(conn, request.query_params.get("title", ""), None, person=person)
    if pid and access.at_least(await access.level_for(person, "page", pid), "view"):
        return RedirectResponse(f"/page/{pid}", status_code=302)
    return _page("No such page", f"<h1>No page called “{html.escape(request.query_params.get('title', ''))}” yet</h1>"
                                 "<p class=meta>Ask Claude to create it, citing its sources.</p>", person)


async def page_view(request: Request, pid: str) -> Response:
    person = await viewer(request)
    if not person:
        return _login_redirect(request)
    if not access.at_least(await access.level_for(person, "page", pid), "view"):
        return _page("Not found", "<h1>Not found</h1>", person)
    p = await db.fetch_one(
        """select p.*, c.name as collection, c.slug, (select email from people where id=p.updated_by) as editor
           from pages p join collections c on c.id=p.collection_id where p.id=%s""", (pid,))
    import datetime as dt

    nb = await links.neighbours(None, "page", pid, person=person)
    out_l = "".join(f"<li>{_ref_link(n['type'], str(n['id']), n['title'])}</li>" for n in nb["outgoing"])
    in_l = "".join(f"<li>{_ref_link(n['type'], str(n['id']), n['title'])}</li>" for n in nb["incoming"])
    stale = bool(p["review_by"] and p["review_by"] < dt.date.today())
    aliases = ", ".join(html.escape(a) for a in p["aliases"]) if p["aliases"] else ""
    body = (f"<div class=grid><article><h1>{html.escape(p['title'])}</h1><div class=meta>"
            f"{_status(p['status'], stale)} {p['kind']} page · <a href='/web/c/{p['slug']}'>{html.escape(p['collection'])}</a>"
            f" · version {p['version']} · updated {p['updated_at']:%d %b %Y}"
            f"{' by ' + html.escape(p['editor']) if p['editor'] else ''}"
            f"{' · also: ' + aliases if aliases else ''}</div>"
            f"<div class=body data-md hidden>{html.escape(p['body'])}</div></article>"
            f"<aside><h2>Links to</h2><ul class=plain>{out_l or '<li class=meta>None</li>'}</ul>"
            f"<h2>Linked from</h2><ul class=plain>{in_l or '<li class=meta>None</li>'}</ul></aside></div>")
    return _page(p["title"], body, person, _MD_SCRIPTS)


async def record_view(request: Request, rid: str) -> Response:
    person = await viewer(request)
    if not person:
        return _login_redirect(request)
    if not access.at_least(await access.level_for(person, "record", rid), "view"):
        return _page("Not found", "<h1>Not found</h1>", person)
    r = await db.fetch_one(
        """select r.*, c.name as collection, c.slug from records r join collections c on c.id=r.collection_id
           where r.id=%s""", (rid,))
    nb = await links.neighbours(None, "record", rid, person=person)
    ev = "".join(f"<li>{_ref_link(n['type'], str(n['id']), n['title'])} <span class=meta>{n['rel']}</span></li>"
                 for n in nb["outgoing"])
    valid = f" · true {r['valid_from'] or '…'} to {r['valid_to'] or 'now'}" if (r["valid_from"] or r["valid_to"]) else ""
    data = ("<h2>Details</h2><ul class=plain>" + "".join(
        f"<li><b>{html.escape(str(k))}</b>: {html.escape(str(v))}</li>" for k, v in r["data"].items()) + "</ul>"
            ) if r["data"] else ""
    sup = f"<p>Replaced by <a href='/record/{r['superseded_by']}'>a newer record</a>.</p>" if r["superseded_by"] else ""
    body = (f"<h1>{html.escape(r['title'])}</h1><div class=meta>{_status(r['status'])} {r['type']} · "
            f"<a href='/web/c/{r['slug']}'>{html.escape(r['collection'])}</a>{valid}</div>{sup}"
            f"<div class=body data-md hidden>{html.escape(r['body'])}</div>{data}"
            f"<h2>Evidence and links</h2><ul class=plain>{ev or '<li class=meta>None</li>'}</ul>")
    return _page(r["title"], body, person, _MD_SCRIPTS)


@mcp.custom_route("/item/{iid}", methods=["GET"], include_in_schema=False)
async def item_view(request: Request) -> Response:
    person = await viewer(request)
    if not person:
        return _login_redirect(request)
    iid = request.path_params["iid"]
    if not access.at_least(await access.level_for(person, "item", iid), "view"):
        return _page("Not found", "<h1>Not found</h1>", person)
    it = await items.get(iid)
    doc = items.document_of(it)
    text = render.document_markdown(doc)[0][:200_000] if doc else "(no readable text)"
    src = f" · <a href='{html.escape(it['url'])}' rel=noopener>open in source</a>" if it["url"] else ""
    body = (f"<h1>{html.escape(it['title'])}</h1><div class=meta>{it['kind']} · "
            f"<a href='/web/c/{it['collection_slug']}'>{html.escape(it['collection_name'])}</a>{src}</div>"
            f"<div class=body data-md hidden>{html.escape(text)}</div>")
    return _page(it["title"], body, person, _MD_SCRIPTS)


@mcp.custom_route("/web/c/{slug}/graph", methods=["GET"], include_in_schema=False)
async def graph_view(request: Request) -> Response:
    person = await viewer(request)
    if not person:
        return _login_redirect(request)
    slug = request.path_params["slug"]
    body = (f"<h1>Graph</h1><p class=meta>Pages, records and sources in <a href='/web/c/{html.escape(slug)}'>"
            f"{html.escape(slug)}</a> and how they link. Click a node to open it.</p><svg id=graph></svg>")
    script = f"""<script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script><script>
fetch('/web/c/{quote(slug)}/graph.json').then(r=>r.json()).then(g=>{{
 const svg=d3.select('#graph'),w=svg.node().clientWidth,h=svg.node().clientHeight;
 const css=getComputedStyle(document.documentElement),fg=css.getPropertyValue('--fg'),mu=css.getPropertyValue('--muted'),
   col={{page:css.getPropertyValue('--accent'),record:css.getPropertyValue('--ok'),item:mu}};
 const sim=d3.forceSimulation(g.nodes).force('link',d3.forceLink(g.links).id(d=>d.id).distance(70))
   .force('charge',d3.forceManyBody().strength(-160)).force('center',d3.forceCenter(w/2,h/2));
 const root=svg.append('g');svg.call(d3.zoom().on('zoom',e=>root.attr('transform',e.transform)));
 const l=root.append('g').selectAll('line').data(g.links).join('line').attr('stroke',mu).attr('stroke-opacity',.5);
 const n=root.append('g').selectAll('g').data(g.nodes).join('g').style('cursor','pointer')
   .on('click',(e,d)=>location.href=d.href).call(d3.drag().on('start',(e,d)=>{{if(!e.active)sim.alphaTarget(.3).restart();d.fx=d.x;d.fy=d.y}})
   .on('drag',(e,d)=>{{d.fx=e.x;d.fy=e.y}}).on('end',(e,d)=>{{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null}}));
 n.append('circle').attr('r',d=>4+Math.min(8,d.degree)).attr('fill',d=>col[d.type]);
 n.append('text').text(d=>d.title).attr('x',10).attr('y',4).attr('font-size',11).attr('fill',fg);
 sim.on('tick',()=>{{l.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y).attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
   n.attr('transform',d=>`translate(${{d.x}},${{d.y}})`)}});
}});</script>"""
    return _page("Graph", body, person, script)


@mcp.custom_route("/web/c/{slug}/graph.json", methods=["GET"], include_in_schema=False)
async def graph_json(request: Request) -> Response:
    person = await viewer(request)
    if not person:
        return JSONResponse({"error": "sign in"}, status_code=401)
    c = await db.fetch_one("select id from collections where slug=%s", (request.path_params["slug"],))
    if not c or not access.at_least(await access.level_for(person, "collection", str(c["id"])), "view"):
        return JSONResponse({"nodes": [], "links": []})
    allowed = await access.visible_items(person, str(c["id"]))
    nodes: dict[str, dict] = {}
    for p in await db.fetch_all("select id, title from pages where collection_id=%s and status<>'archived'", (c["id"],)):
        nodes[f"page:{p['id']}"] = {"id": f"page:{p['id']}", "title": p["title"], "type": "page", "href": f"/page/{p['id']}"}
    for r in await db.fetch_all("select id, title from records where collection_id=%s and status<>'superseded'", (c["id"],)):
        nodes[f"record:{r['id']}"] = {"id": f"record:{r['id']}", "title": r["title"], "type": "record",
                                      "href": f"/record/{r['id']}"}
    ids = [k.split(":", 1)[1] for k in nodes]
    edges = await db.fetch_all(
        """select from_type, from_id, to_type, to_id from links
           where from_id = any(%s::uuid[]) or to_id = any(%s::uuid[])""", (ids, ids))
    out = []
    for e in edges:
        a, b = f"{e['from_type']}:{e['from_id']}", f"{e['to_type']}:{e['to_id']}"
        for key, t, i in ((a, e["from_type"], e["from_id"]), (b, e["to_type"], e["to_id"])):
            if key not in nodes and t == "item" and (allowed is None or str(i) in allowed):
                row = await db.fetch_one("select title, collection_id from items where id=%s", (i,))
                if row and str(row["collection_id"]) == str(c["id"]):
                    nodes[key] = {"id": key, "title": row["title"], "type": "item", "href": f"/item/{i}"}
        if a in nodes and b in nodes:
            out.append({"source": a, "target": b})
    deg: dict[str, int] = {}
    for e in out:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    for k, n in nodes.items():
        n["degree"] = deg.get(k, 0)
    return JSONResponse({"nodes": list(nodes.values()), "links": out})


@mcp.custom_route("/web/review", methods=["GET"], include_in_schema=False)
async def web_review(request: Request) -> Response:
    person = await viewer(request)
    if not person:
        return _login_redirect(request)
    q = await org.review_queue(person)

    def ul(rows: list[str]) -> str:
        return "<ul class=plain>" + ("".join(rows) or "<li class=meta>Nothing here</li>") + "</ul>"

    body = ("<h1>Review</h1><p class=meta>For leads and owners: ask Claude to do the weekly upkeep "
            "(the nebula-upkeep skill), or act on these directly through Claude.</p>"
            "<h2>Pages past their review date</h2>" + ul([
                f"<li><a href='/page/{p['id'].split(':')[1]}'>{html.escape(p['title'])}</a> "
                f"<span class=meta>due {p['review_was_due']}</span></li>" for p in q["stale_pages"]])
            + "<h2>Proposed edits</h2>" + ul([
                f"<li>{html.escape(p['page'])} <span class=meta>by {html.escape(p['by'] or '')}: "
                f"{html.escape(p['note'] or '')}</span></li>" for p in q["open_proposals"]])
            + "<h2>Draft records</h2>" + ul([
                f"<li><a href='/record/{r['id'].split(':')[1]}'>{html.escape(r['title'])}</a> "
                f"<span class=meta>{r['type']}</span></li>" for r in q["draft_records"]])
            + "<h2>Draft skills</h2>" + ul([
                f"<li>{html.escape(s['name'])} <span class=meta>{html.escape(s['description'])}</span></li>"
                for s in q["draft_skills"]]))
    return _page("Review", body, person)
