# ftrack MCP server

A **Model Context Protocol** server that gives LLM agents (Claude Desktop, Claude Code, Cursor, …) broad,
typed access to the **ftrack Studio** production-tracking API.

Why another one? The only existing ftrack MCP on GitHub is an early, **unlicensed** single-author experiment.
This one is **MIT-licensed**, **broad-coverage**, and tested live against an ftrack Studio trial.

## Coverage philosophy
ftrack's API is a **generic query + CRUD over a flexible schema** (143 entity types). So coverage comes from
two layers:
- **Generic power tools** — `query`, `query_one`, `create`, `update`, `delete` — reach **every entity type**
  via ftrack's query language. This alone is ~full coverage.
- **Typed convenience tools** — projects, structure, tasks, statuses, assignments, notes, lists, time logs,
  thumbnails, users, and full **schema introspection** — make the common production ops one call each.

**28 tools** in total (see below).

## Install
```bash
pip install -r requirements.txt        # fastmcp, ftrack-python-api, requests
```

## Configure (credentials)
Set three env vars (or pass them in your MCP client config):
| var | value |
|---|---|
| `FTRACK_SERVER` | `https://yourstudio.ftrackapp.com` |
| `FTRACK_API_USER` | your login email |
| `FTRACK_API_KEY` | a **Personal API key** — ftrack ▸ avatar ▸ My account ▸ **Security settings** ▸ *Create API key* |

## Run
```bash
python3 server.py            # stdio transport
```

### Wire into Claude Code
```bash
claude mcp add ftrack \
  -e FTRACK_SERVER=https://yourstudio.ftrackapp.com \
  -e FTRACK_API_USER=you@studio.com \
  -e FTRACK_API_KEY=*** \
  -- python3 /abs/path/to/ftrack-mcp/server.py
```
(Tools appear as `mcp__ftrack__*` on the next session.) For **Claude Desktop / Cursor**, add the same command +
env to the app's `mcpServers` config.

## Tools
**Generic (full reach):** `query` · `query_one` · `create` · `update` · `delete`
**Schema / discovery:** `list_entity_types` · `get_entity_schema` · `list_project_schemas` · `list_statuses` ·
`list_task_types` · `list_object_types` · `list_priorities` · `list_custom_attributes`
**Projects / structure:** `list_projects` · `get_project` · `create_project` · `list_children` · `list_tasks` ·
`create_task`
**Ops:** `set_status` · `assign_task` · `add_note` · `get_notes` · `list_lists` · `log_time` · `set_thumbnail`
**Users / meta:** `whoami` · `list_users`

### Examples (what an agent would call)
- *"every in-progress lighting shot"* → `query("Task where type.name is \"Lighting\" and status.name is \"In progress\"", ["name","parent.name"])`
- *"add a note to this shot"* → `add_note("Shot", "<id>", "Looks good, ship it")`
- *"what fields does a Shot have?"* → `get_entity_schema("Shot")`
- *"make a task"* → `create_task("<shot_id>", "Compositing", status="Ready to start")`

## Notes
- Reads return entities serialized to the **fields you request** (dot-paths like `status.name`), to avoid
  lazy-loading huge relations. Writes auto-resolve `{"id": "..."}` references (parent → `Context`,
  project → `Project`, status → `Status`, type → `Type`, …).
- Validated live: all 28 tools register; query/schema/create/update/delete/notes/status round-trips pass.
- A TS port over the official `ftrack-javascript` SDK is straightforward if you want it in-stack.

MIT © 2026 John Huikku
