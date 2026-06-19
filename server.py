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
import os, tempfile, datetime
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


def create(entity_type: str, data: dict, commit: bool = True) -> dict:
    """Create any entity. `data` maps attributes to values; entity references may be passed as
    {"id": "..."} or {"__entity_type__": "Type", "id": "..."} and are resolved automatically.
    e.g. create("Sequence", {"name":"sq010","parent":{"id":"<project_id>"}})."""
    e = session().create(entity_type, _resolve_refs(data))
    if commit:
        session().commit()
    return ser(e, list(data.keys()) + ["id"])


def update(entity_type: str, entity_id: str, data: dict, commit: bool = True) -> dict:
    """Update an entity by id. `data` = attributes to set (refs as {"id": ...})."""
    e = session().get(entity_type, entity_id)
    for k, v in _resolve_refs(data).items():
        e[k] = v
    if commit:
        session().commit()
    return ser(e, list(data.keys()) + ["id"])


def delete(entity_type: str, entity_id: str, commit: bool = True) -> dict:
    """Delete an entity by id."""
    session().delete(session().get(entity_type, entity_id))
    if commit:
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


def create_project(name: str, full_name: str, schema: str = "VFX") -> dict:
    """Create a project. `schema` is a project-schema name (see list_project_schemas)."""
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


def create_task(parent_id: str, task_type: str, name: str = None, status: str = None) -> dict:
    """Create a task under a parent (shot/asset). task_type & status are names."""
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
def set_status(entity_type: str, entity_id: str, status: str) -> dict:
    """Set an entity's status by status name."""
    e = session().get(entity_type, entity_id)
    e["status"] = session().query('Status where name is "%s"' % status).one()
    session().commit()
    return ser(e, ["name", "status.name", "id"])


def assign_task(task_id: str, user_id: str) -> dict:
    """Assign a user to a task (creates an Appointment)."""
    session().create("Appointment", {"context": session().get("Task", task_id),
                                      "resource": session().get("User", user_id), "type": "assignment"})
    session().commit()
    return {"ok": True, "task_id": task_id, "user_id": user_id}


def add_note(entity_type: str, entity_id: str, text: str, author_id: str = None) -> dict:
    """Add a note to any entity. author_id defaults to the API user."""
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


def log_time(task_id: str, duration_seconds: int, user_id: str = None, start: str = None) -> dict:
    """Log time on a task (TimeLog). duration in seconds."""
    user = session().get("User", user_id) if user_id else \
        session().query('User where username is "%s"' % _env("FTRACK_API_USER")).first()
    data = {"context_id": task_id, "user_id": user["id"], "duration": duration_seconds}
    if start:
        data["start"] = start
    tl = session().create("Timelog", data)
    session().commit()
    return ser(tl, ["duration", "id"])


def set_thumbnail(entity_type: str, entity_id: str, image_url_or_path: str) -> dict:
    """Set an entity's thumbnail from a local path or a URL."""
    e = session().get(entity_type, entity_id)
    path = image_url_or_path
    if image_url_or_path.startswith("http"):
        data = requests.get(image_url_or_path, timeout=30).content
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(data); path = f.name
    e.create_thumbnail(path)
    session().commit()
    return {"ok": True, "entity_id": entity_id}


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


# ---- register every function above as an MCP tool -----------------------------------------
for _fn in (query, query_one, create, update, delete,
            list_entity_types, get_entity_schema, list_project_schemas, list_statuses,
            list_task_types, list_object_types, list_priorities, list_custom_attributes,
            list_projects, get_project, create_project, list_children, list_tasks, create_task,
            set_status, assign_task, add_note, get_notes, list_lists, log_time, set_thumbnail,
            whoami, list_users):
    mcp.tool(_fn)


if __name__ == "__main__":
    mcp.run()
