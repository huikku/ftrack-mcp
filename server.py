#!/usr/bin/env python3
"""ftrack MCP server — broad coverage of the ftrack Studio API for LLM agents.

Design: ftrack's API is a *generic query + CRUD over a flexible schema*, so a few
**generic power tools** (`query`/`create`/`update`/`delete`) give ~100% reach across all
143 entity types, and **typed convenience tools** make the common production ops
agent-friendly. MIT licensed.

Config (env or MCP client config):
  FTRACK_SERVER   e.g. https://yourstudio.ftrackapp.com
  FTRACK_API_USER e.g. you@studio.com
  FTRACK_API_KEY  a Personal API key (Security settings -> Create API key)

Run:  python3 server.py        (stdio transport, for Claude Desktop / Cursor / Claude Code)
"""
import os, tempfile, datetime, json
import requests
import ftrack_api
from fastmcp import FastMCP

mcp = FastMCP("ftrack")
_session = None


def _env(name, default=None):
    v = os.environ.get(name)
    if v:
        return v
    # fall back to a sibling .env (dev convenience) — never required in production
    for p in (".env",):
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].split(" #", 1)[0].strip().strip('"').strip("'")
    return default


def session():
    global _session
    if _session is None:
        _session = ftrack_api.Session(
            server_url=_env("FTRACK_SERVER"), api_key=_env("FTRACK_API_KEY"), api_user=_env("FTRACK_API_USER"))
    return _session


# ---- serialization (avoid lazy-loading huge relations; caller picks fields) ----------------
def _scalar(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (datetime.datetime, datetime.date)):
        return str(v)
    if hasattr(v, "entity_type"):  # an entity reference
        try:
            return {"__entity_type__": v.entity_type, "id": v.get("id"), "name": v.get("name")}
        except Exception:
            return {"__entity_type__": getattr(v, "entity_type", "?")}
    if hasattr(v, "__iter__"):     # a collection -> count only, to avoid heavy loads
        try:
            return f"<collection len={len(v)}>"
        except Exception:
            return "<collection>"
    return str(v)


def _path(entity, dotted):
    cur = entity
    for part in dotted.split("."):
        if cur is None:
            return None
        try:
            cur = cur[part]
        except Exception:
            return None
    return _scalar(cur)


def ser(entity, fields=None):
    out = {"__entity_type__": entity.entity_type}
    try:
        out["id"] = entity["id"]
    except Exception:
        pass
    if not fields:
        fields = ["name"]
    for f in fields:
        out[f] = _path(entity, f)
    return out


def ser_list(entities, fields=None, limit=200):
    return [ser(e, fields) for e in list(entities)[:limit]]


# ---- dry-run modes: plan (client-side echo) · preflight (real reads + schema staging, no write) --
def _mode(dry_run):
    if isinstance(dry_run, str):
        d = dry_run.lower()
        return "preflight" if d == "preflight" else ("live" if d in ("", "false", "no") else "plan")
    return "plan" if dry_run else "live"


def _planlog(entry):
    entry = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), **entry}
    path = os.environ.get("MCP_PLAN_LOG")
    if path:
        try:
            with open(path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass
    return entry


def _ft_check_refs(data):
    """Preflight: read every {id} / {__entity_type__,id} link in `data` and report found/name."""
    reads, conflicts = {}, []
    for k, v in (data or {}).items():
        if isinstance(v, dict) and "id" in v:
            et = v.get("__entity_type__") or _ref_type(k)
            if not et:
                continue
            try:
                r = session().query('%s where id is "%s"' % (et, v["id"])).first()
                reads[k] = {"found": bool(r), "name": _path(r, "name") if r else None}
                if not r:
                    conflicts.append("%s %s %s not found" % (k, et, v["id"]))
            except Exception as e:
                reads[k] = {"found": False, "error": repr(e)[:80]}
                conflicts.append("%s could not be resolved" % k)
    return reads, conflicts


# =====================================================================================
#  GENERIC POWER TOOLS  (full API reach)
# =====================================================================================
def query(expression: str, fields: list[str] | None = None, limit: int = 100) -> list[dict]:
    """Run any ftrack query and return matching entities.

    `expression` is an ftrack query, e.g. "Task where project.name is \\"My Show\\" and status.name is \\"In progress\\"",
    or with projections: "select name, status.name, bid from Shot where parent.name is \\"sq010\\"".
    `fields` are the (dot-)attributes to return per entity (e.g. ["name","status.name","type.name"]).
    This single tool covers all reads across every ftrack entity type.
    """
    return ser_list(session().query(expression), fields, limit)


def query_one(expression: str, fields: list[str] | None = None) -> dict | None:
    """Run a query and return the first match (or null)."""
    r = session().query(expression).first()
    return ser(r, fields) if r else None


def create(entity_type: str, data: dict, dry_run=False) -> dict:
    """Create any entity. `data` maps attributes to values; entity references may be passed as
    {"id": "..."} or {"__entity_type__": "Type", "id": "..."} and are resolved automatically.
    e.g. create("Sequence", {"name":"sq010","parent":{"id":"<project_id>"}}).
    `dry_run`: false = write · "plan"/true = echo the intent (no server contact) · "preflight" = resolve
    every reference against live data AND stage the create in the session to run ftrack's own schema/required
    validation, then roll back (writes nothing). Set MCP_PLAN_LOG to capture a JSONL plan file."""
    mode = _mode(dry_run)
    if mode == "plan":
        return _planlog({"dry_run": "plan", "would": "create", "entity_type": entity_type, "input": data})
    if mode == "preflight":
        reads, conflicts = _ft_check_refs(data)
        staged = {}
        try:  # bonus: ftrack validates schema/type/required at create-time; stage then roll back
            session().create(entity_type, _resolve_refs(data))
            staged = {"schema_valid": True}
        except Exception as ex:
            staged = {"schema_valid": False, "error": str(ex)[:120]}
            conflicts.append("schema: " + str(ex)[:100])
        finally:
            try:
                session().rollback()
            except Exception:
                pass
        return _planlog({"dry_run": "preflight", "would": "create", "entity_type": entity_type, "input": data,
                         "reads": reads, "staged": staged, "conflicts": conflicts,
                         "verdict": "would_fail" if conflicts else "ok"})
    e = session().create(entity_type, _resolve_refs(data))
    session().commit()
    return ser(e, list(data.keys()) + ["id"])


def update(entity_type: str, entity_id: str, data: dict, dry_run=False) -> dict:
    """Update an entity by id. `data` = attributes to set (refs as {"id": ...}).
    `dry_run`: false = write · "plan"/true = echo · "preflight" = read the current entity and return a real
    before→after diff + resolve references (no write). Logged to MCP_PLAN_LOG."""
    mode = _mode(dry_run)
    if mode == "plan":
        return _planlog({"dry_run": "plan", "would": "update", "entity_type": entity_type,
                         "entity_id": entity_id, "input": data})
    if mode == "preflight":
        e = session().query('%s where id is "%s"' % (entity_type, entity_id)).first()
        reads, conflicts = _ft_check_refs(data)
        if not e:
            conflicts.append("%s %s not found" % (entity_type, entity_id))

        def _cur(k):
            try:
                return _scalar(e[k]) if e else None
            except Exception:
                return None
        change = {k: {"from": _cur(k), "to": (v.get("id") if isinstance(v, dict) else v)}
                  for k, v in (data or {}).items()}
        return _planlog({"dry_run": "preflight", "would": "update", "entity_type": entity_type,
                         "entity_id": entity_id, "exists": bool(e), "change": change, "reads": reads,
                         "conflicts": conflicts, "verdict": "would_fail" if conflicts else "ok"})
    e = session().get(entity_type, entity_id)
    for k, v in _resolve_refs(data).items():
        e[k] = v
    session().commit()
    return ser(e, list(data.keys()) + ["id"])


def delete(entity_type: str, entity_id: str, dry_run=False) -> dict:
    """Delete an entity by id.
    `dry_run`: false = delete · "plan"/true = echo · "preflight" = confirm the target exists (live read) and
    show what would be deleted (no write). Logged to MCP_PLAN_LOG."""
    mode = _mode(dry_run)
    if mode == "plan":
        return _planlog({"dry_run": "plan", "would": "delete", "entity_type": entity_type, "entity_id": entity_id})
    if mode == "preflight":
        e = session().query('%s where id is "%s"' % (entity_type, entity_id)).first()
        conflicts = [] if e else ["%s %s not found" % (entity_type, entity_id)]
        return _planlog({"dry_run": "preflight", "would": "delete", "entity_type": entity_type,
                         "entity_id": entity_id, "exists": bool(e), "name": _path(e, "name") if e else None,
                         "conflicts": conflicts, "verdict": "would_fail" if conflicts else "ok"})
    session().delete(session().get(entity_type, entity_id))
    session().commit()
    return {"ok": True, "deleted": {"__entity_type__": entity_type, "id": entity_id}}


def _resolve_refs(data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if isinstance(v, dict) and "id" in v:
            et = v.get("__entity_type__") or _ref_type(k)
            resolved = session().query('%s where id is "%s"' % (et, v["id"])).first() if et else None
            out[k] = resolved if resolved is not None else v
        else:
            out[k] = v
    return out


def _ref_type(key):
    # base/polymorphic types so a parent can be a Project OR a Sequence/Shot/etc.
    return {"parent": "Context", "project": "Project", "status": "Status", "type": "Type",
            "author": "User", "resource": "User", "user": "User", "context": "Context",
            "asset": "Asset"}.get(key)


# =====================================================================================
#  SCHEMA / DISCOVERY
# =====================================================================================
def list_entity_types() -> list[str]:
    """List all entity types in this ftrack schema (143+)."""
    return sorted(session().types.keys())


def get_entity_schema(entity_type: str) -> dict:
    """Get an entity type's attributes (names + kinds) — for building queries/creates."""
    et = session().types.get(entity_type)
    if not et:
        return {"error": f"unknown entity type {entity_type}"}
    attrs = {}
    for a in et.attributes:
        attrs[a.name] = type(a).__name__.replace("Attribute", "").lower() or "scalar"
    return {"entity_type": entity_type, "attributes": attrs}


def list_project_schemas() -> list[dict]:
    """Project schemas + their task types / statuses / object types (the workflow config)."""
    out = []
    for ps in session().query("ProjectSchema").all():
        try:
            out.append({"name": ps["name"],
                        "task_types": [t["name"] for t in ps.get_types("Task")],
                        "task_statuses": [s["name"] for s in ps.get_statuses("Task")],
                        "asset_build_types": [t["name"] for t in ps.get_types("AssetBuild")]})
        except Exception as e:
            out.append({"name": ps["name"], "error": str(e)})
    return out


def list_statuses() -> list[dict]:
    """All workflow statuses (name + state)."""
    return [{"name": s["name"], "state": _path(s, "state.name"), "color": s.get("color")}
            for s in session().query("Status").all()]


def list_task_types() -> list[str]:
    """All task types."""
    return [t["name"] for t in session().query("Type").all()]


def list_object_types() -> list[str]:
    """All object types (Shot, Sequence, Asset Build, Episode, Scene, Milestone, ...)."""
    return [o["name"] for o in session().query("ObjectType").all()]


def list_priorities() -> list[str]:
    """All priorities."""
    return [p["name"] for p in session().query("Priority").all()]


def list_custom_attributes() -> list[dict]:
    """Custom attribute definitions (schema-as-data): name, type, entity, default."""
    out = []
    for c in session().query("CustomAttributeConfiguration").all():
        out.append({"key": c["key"], "label": c.get("label"),
                    "type": _path(c, "type.name"), "default": c.get("default"),
                    "entity_type": c.get("entity_type"), "object_type": _path(c, "object_type.name")})
    return out


# =====================================================================================
#  TYPED CONVENIENCE — projects / structure
# =====================================================================================
def list_projects(include_closed: bool = False) -> list[dict]:
    """List projects (name, full_name, status)."""
    q = "Project" if include_closed else "Project where status is active"
    return ser_list(session().query(q), ["name", "full_name", "status"])


def get_project(name_or_id: str) -> dict | None:
    """Get a project by name or id."""
    r = session().query('Project where name is "%s" or id is "%s"' % (name_or_id, name_or_id)).first()
    return ser(r, ["name", "full_name", "status", "project_schema.name"]) if r else None


def create_project(name: str, full_name: str, schema: str = "VFX", dry_run: bool = False) -> dict:
    """Create a project. `schema` is a project-schema name (see list_project_schemas).
    `dry_run=true` previews without committing."""
    if dry_run:
        return {"dry_run": True, "would": "create project", "name": name, "full_name": full_name, "schema": schema}
    ps = session().query('ProjectSchema where name is "%s"' % schema).one()
    p = session().create("Project", {"name": name, "full_name": full_name, "project_schema": ps})
    session().commit()
    return ser(p, ["name", "full_name", "id"])


def list_children(parent_id: str, object_type: str | None = None, limit: int = 200) -> list[dict]:
    """List child contexts of a project/sequence/etc. Optionally filter by object type (e.g. "Shot")."""
    q = 'TypedContext where parent.id is "%s"' % parent_id
    if object_type:
        q += ' and object_type.name is "%s"' % object_type
    return ser_list(session().query(q), ["name", "object_type.name", "status.name"], limit)


def list_tasks(project_id: str = None, entity_id: str = None, limit: int = 200) -> list[dict]:
    """List tasks for a project (project_id) or under a specific entity (entity_id)."""
    if entity_id:
        q = 'Task where parent.id is "%s"' % entity_id
    else:
        q = 'Task where project.id is "%s"' % project_id
    return ser_list(session().query(q), ["name", "type.name", "status.name", "bid", "parent.name"], limit)


def create_task(parent_id: str, task_type: str, name: str = None, status: str = None,
                dry_run: bool = False) -> dict:
    """Create a task under a parent (shot/asset). task_type & status are names.
    `dry_run=true` previews without committing."""
    if dry_run:
        return {"dry_run": True, "would": "create task", "parent_id": parent_id,
                "task_type": task_type, "name": name, "status": status}
    parent = session().get("TypedContext", parent_id)
    data = {"name": name or task_type, "parent": parent,
            "type": session().query('Type where name is "%s"' % task_type).one()}
    if status:
        st = session().query('Status where name is "%s"' % status).first()
        if st:
            data["status"] = st
    t = session().create("Task", data)
    session().commit()
    return ser(t, ["name", "type.name", "status.name", "id"])


# =====================================================================================
#  status / assignment / notes / lists / time / media / users
# =====================================================================================
def set_status(entity_type: str, entity_id: str, status: str, dry_run=False) -> dict:
    """Set an entity's status by status name. `dry_run`: false = write · "plan"/true = echo ·
    "preflight" = read the entity + validate the status name against the live schema and show the
    current→new status (no write). Logged to MCP_PLAN_LOG."""
    mode = _mode(dry_run)
    if mode == "plan":
        return _planlog({"dry_run": "plan", "would": "set status", "entity_type": entity_type,
                         "entity_id": entity_id, "status": status})
    if mode == "preflight":
        e = session().query('%s where id is "%s"' % (entity_type, entity_id)).first()
        st = session().query('Status where name is "%s"' % status).first()
        cur = _path(e, "status.name") if e else None
        conflicts = []
        if not e:
            conflicts.append("%s %s not found" % (entity_type, entity_id))
        if not st:
            conflicts.append("status %r not found" % status)
        return _planlog({"dry_run": "preflight", "would": "set status", "entity_type": entity_type,
                         "entity_id": entity_id, "exists": bool(e),
                         "change": {"status": "%s → %s" % (cur, status)},
                         "conflicts": conflicts, "verdict": "would_fail" if conflicts else "ok"})
    e = session().get(entity_type, entity_id)
    e["status"] = session().query('Status where name is "%s"' % status).one()
    session().commit()
    return ser(e, ["name", "status.name", "id"])


def assign_task(task_id: str, user_id: str, dry_run: bool = False) -> dict:
    """Assign a user to a task (creates an Appointment). `dry_run=true` previews without committing."""
    if dry_run:
        return {"dry_run": True, "would": "assign task", "task_id": task_id, "user_id": user_id}
    session().create("Appointment", {"context": session().get("Task", task_id),
                                      "resource": session().get("User", user_id), "type": "assignment"})
    session().commit()
    return {"ok": True, "task_id": task_id, "user_id": user_id}


def add_note(entity_type: str, entity_id: str, text: str, author_id: str = None,
             dry_run: bool = False) -> dict:
    """Add a note to any entity. author_id defaults to the API user. `dry_run=true` previews."""
    if dry_run:
        return {"dry_run": True, "would": "add note", "entity_type": entity_type,
                "entity_id": entity_id, "text": text}
    e = session().get(entity_type, entity_id)
    author = session().get("User", author_id) if author_id else \
        session().query('User where username is "%s"' % _env("FTRACK_API_USER")).first()
    note = e.create_note(text, author)
    session().commit()
    return ser(note, ["content", "id"])


def get_notes(entity_type: str, entity_id: str, limit: int = 50) -> list[dict]:
    """Get notes on an entity."""
    e = session().get(entity_type, entity_id)
    return [{"content": n.get("content"), "author": _path(n, "author.username"),
             "date": str(n.get("date"))} for n in list(e["notes"])[:limit]]


def list_lists(project_id: str) -> list[dict]:
    """Review/client lists in a project."""
    return ser_list(session().query('List where project.id is "%s"' % project_id), ["name", "category.name"])


def log_time(task_id: str, duration_seconds: int, user_id: str = None, start: str = None,
             dry_run: bool = False) -> dict:
    """Log time on a task (TimeLog). duration in seconds. `dry_run=true` previews without committing."""
    if dry_run:
        return {"dry_run": True, "would": "log time", "task_id": task_id, "duration_seconds": duration_seconds}
    user = session().get("User", user_id) if user_id else \
        session().query('User where username is "%s"' % _env("FTRACK_API_USER")).first()
    data = {"context_id": task_id, "user_id": user["id"], "duration": duration_seconds}
    if start:
        data["start"] = start
    tl = session().create("Timelog", data)
    session().commit()
    return ser(tl, ["duration", "id"])


def set_thumbnail(entity_type: str, entity_id: str, image_url_or_path: str, dry_run: bool = False) -> dict:
    """Set an entity's thumbnail from a local path or a URL. `dry_run=true` previews without committing."""
    if dry_run:
        return {"dry_run": True, "would": "set thumbnail", "entity_type": entity_type,
                "entity_id": entity_id, "source": image_url_or_path}
    e = session().get(entity_type, entity_id)
    path = image_url_or_path
    if image_url_or_path.startswith("http"):
        data = requests.get(image_url_or_path, timeout=30).content
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(data); path = f.name
    e.create_thumbnail(path)
    session().commit()
    return {"ok": True, "entity_id": entity_id}


def create_version(parent_id: str, name: str = None, task_id: str = None, asset_type: str = None,
                   dry_run: bool = False) -> dict:
    """Create an AssetVersion under a context (shot/asset) — ftrack's review unit. ftrack AssetVersions live
    under an Asset, so the parent Asset is created if missing. `asset_type` is an AssetType name (default:
    first available); link a task with `task_id`. Pair with `upload_review_media` to attach a movie.
    `dry_run=true` previews without committing."""
    if dry_run:
        return {"dry_run": True, "would": "create version", "parent_id": parent_id,
                "name": name, "task_id": task_id, "asset_type": asset_type}
    s = session()
    parent = s.query('Context where id is "%s"' % parent_id).first()
    at = (s.query('AssetType where name is "%s"' % asset_type).first() if asset_type
          else s.query("AssetType").first())
    aname = name or (parent["name"] if parent else "asset")
    asset = s.query('Asset where parent.id is "%s" and name is "%s"' % (parent_id, aname)).first()
    if not asset:
        asset = s.create("Asset", {"name": aname, "parent": parent, "type": at})
    data = {"asset": asset}
    if task_id:
        data["task"] = s.get("Task", task_id)
    ver = s.create("AssetVersion", data)
    s.commit()
    return ser(ver, ["version", "id"])


def upload_review_media(version_id: str, path: str, dry_run: bool = False) -> dict:
    """Upload a movie/image to an AssetVersion as **web-reviewable** media — ftrack encodes it
    (produces the ftrackreview component). Use this to carry version media INTO ftrack during a migration.
    `dry_run=true` previews without uploading."""
    if dry_run:
        return {"dry_run": True, "would": "upload review media", "version_id": version_id, "path": path}
    ver = session().get("AssetVersion", version_id)
    ver.encode_media(path)
    session().commit()
    return {"ok": True, "version_id": version_id, "encoded": path}


def whoami() -> dict:
    """The authenticated API user + server info."""
    s = session()
    u = s.query('User where username is "%s"' % s.api_user).first()
    return {"api_user": s.api_user, "server": _env("FTRACK_SERVER"),
            "user": ser(u, ["username", "first_name", "last_name", "is_active"]) if u else None,
            "entity_types": len(s.types)}


def list_users(limit: int = 200) -> list[dict]:
    """List users."""
    return ser_list(session().query("User"), ["username", "first_name", "last_name", "is_active"], limit)


# canonical status buckets — the shared vocabulary for cross-tracker diff/verify
_CANON = {"not started": "todo", "ready to start": "todo", "on hold": "wip", "in progress": "wip",
          "wip": "wip", "pending review": "review", "awaiting client": "review", "revise": "review",
          "needs attention": "wip", "client approved": "approved", "approved": "approved",
          "omitted": "todo", "production": "wip", "post-production": "wip"}


def project_summary(project_id: str) -> dict:
    """A **normalized snapshot** of a project for cross-tracker verify/diff: entity counts plus, per shot,
    its thumbnail flag and per-task **canonical** status (todo/wip/done/review/approved). Every tracker MCP
    emits the same shape, so two summaries can be diffed directly. (ftrack has no shot↔asset casting model,
    so `cast` is always empty here.) Read-only."""
    s = session()
    proj = s.query('Project where id is "%s"' % project_id).first()
    seqs = s.query('Sequence where project.id is "%s"' % project_id).all()
    assets = s.query('select name, thumbnail_id from AssetBuild where project.id is "%s"' % project_id).all()
    shots = s.query('select name, thumbnail_id from Shot where project.id is "%s"' % project_id).all()
    tasks = s.query('select type.name, status.name, parent.name, parent.object_type.name '
                    'from Task where project.id is "%s"' % project_id).all()

    def canon(v):
        return _CANON.get((v or "").lower(), (v or "").lower())
    sm = {sh["name"]: {"cast": [], "thumbnail": bool(sh["thumbnail_id"]), "tasks": {}} for sh in shots}
    for t in tasks:
        par = t["parent"]
        if par and _path(par, "object_type.name") == "Shot" and par["name"] in sm:
            sm[par["name"]]["tasks"][_path(t, "type.name")] = canon(_path(t, "status.name"))
    return {"tracker": "ftrack", "project": {"name": proj["name"] if proj else None, "id": project_id},
            "counts": {"sequences": len(seqs), "assets": len(assets), "shots": len(shots), "tasks": len(tasks)},
            "shots": sm, "assets": {a["name"]: {"thumbnail": bool(a["thumbnail_id"])} for a in assets}}


# ---- register every function above as an MCP tool -----------------------------------------
for _fn in (query, query_one, create, update, delete,
            list_entity_types, get_entity_schema, list_project_schemas, list_statuses,
            list_task_types, list_object_types, list_priorities, list_custom_attributes,
            list_projects, get_project, create_project, list_children, list_tasks, create_task,
            set_status, assign_task, add_note, get_notes, list_lists, log_time, set_thumbnail,
            create_version, upload_review_media, project_summary, whoami, list_users):
    mcp.tool(_fn)


if __name__ == "__main__":
    mcp.run()
