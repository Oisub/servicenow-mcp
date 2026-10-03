"""ServiceNow MCP server focused on development, debugging and keeping instances in sync."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from .client import ServiceNowError, SNClient
from . import auth, config, debugtools, keepalive, updateset
from .config import ConfigError, load_instances

mcp = MCPServer(
    "servicenow",
    instructions=(
        "Tools for ServiceNow development and environment setup across multiple instances. "
        "Every tool takes an optional `instance` (name from list_instances); omitted means the "
        "current default. Queries use ServiceNow encoded query syntax (e.g. 'active=true^nameLIKEfoo'). "
        "Before writing to a table you have not used, call describe_table to get real field names. "
        "Use run_script for anything the REST tools cannot do (GlideRecord, gs.*, GlideAggregate).\n\n"
        "Browser use (Claude in Chrome) - keep it rare: these tools are faster and more reliable for "
        "data, schema, scripts, logs, ACLs, flows, emails and ATF. Open the browser only when the "
        "answer depends on what the UI renders or does on the client: form layout, UI policies, client "
        "scripts, UI action visibility, Service Portal / Workspace pages, the Flow Designer canvas, ATF "
        "tests with UI steps (client test runner), or when the user asks to see something. Then get the "
        "exact URL with ui_link (or a ui_url from a tool result), open one tab, check that one thing, "
        "report, and stop - don't click around exploring. Verify data changes through these tools, not "
        "by reading pages. The browser must already be logged in to the instance: if a login page "
        "appears, ask the user to log in; never type credentials."
    ),
)

logging.getLogger("httpx").setLevel(logging.WARNING)

_clients: dict[str, SNClient] = {}
_session_default: str | None = None

Display = Literal["both", "value", "display"]
SYSTEM_FIELDS = {
    "sys_created_on", "sys_created_by", "sys_updated_on", "sys_updated_by",
    "sys_mod_count", "sys_tags",
}


# ---------------------------------------------------------------- helpers

async def _open_instance_dialog(name: str = "") -> dict:
    """Show the add-instance dialog on the user's desktop and wait for it (max 10 minutes)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "servicenow_mcp.gui", name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        return {"saved": False, "reason": "Dialog timed out after 10 minutes"}
    try:
        result = json.loads(out.decode("utf-8").strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise ServiceNowError(f"Dialog failed: {err.decode('utf-8', 'replace')[-500:]}")
    if result.get("saved"):
        _clients.pop(result["name"], None)
    else:
        result["reason"] = "User cancelled"
    return result


async def _client(instance: str | None) -> SNClient:
    instances, default = load_instances()
    if not instances:
        # First use: ask for an instance right away instead of failing.
        if not (await _open_instance_dialog()).get("saved"):
            raise ConfigError("No ServiceNow instance configured and the setup dialog was cancelled. "
                              "Call add_instance when the user is ready.")
        instances, default = load_instances()
    name = instance or _session_default or default
    if not name:
        raise ConfigError(f"No default instance. Pass `instance` (one of: {', '.join(instances)}).")
    if name not in instances:
        raise ConfigError(f"Unknown instance '{name}'. Known: {', '.join(instances) or '(none)'}")
    cached = _clients.get(name)
    if cached is None or cached.instance != instances[name]:
        try:
            cached = SNClient(instances[name])
        except ConfigError:
            # Registered but no password stored on this machine: ask for it.
            if not (await _open_instance_dialog(name)).get("saved"):
                raise
            instances, _ = load_instances()
            cached = SNClient(instances[name])
        _clients[name] = cached
    return cached


def _display_param(display: Display) -> str:
    return {"both": "all", "value": "false", "display": "true"}[display]


def _compact(record: dict) -> dict:
    """Collapse display_value=all output: identical value/display -> scalar, else {value, display}."""
    out = {}
    for key, val in record.items():
        if isinstance(val, dict) and "value" in val:
            v, d = val.get("value"), val.get("display_value")
            out[key] = v if d in (None, v) else {"value": v, "display": d}
        else:
            out[key] = val
    return out


def _fields(fields: list[str] | str | None) -> str | None:
    if fields is None:
        return None
    return fields if isinstance(fields, str) else ",".join(fields)


def _short(value: Any, limit: int = 300) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... ({len(value)} chars)"
    return value


async def _my_user_sys_id(c: SNClient) -> str:
    rows = await c.table_get("sys_user", sysparm_query=f"user_name={c.instance.username}",
                             sysparm_fields="sys_id", sysparm_limit=1)
    if not rows:
        raise ServiceNowError(f"User '{c.instance.username}' not found in sys_user")
    return rows[0]["sys_id"]


# --------------------------------------------------------------- instances

@mcp.tool()
async def list_instances() -> list[dict]:
    """List configured ServiceNow instances and which one is the current default."""
    instances, default = load_instances()
    current = _session_default or default
    return [
        {"name": i.name, "url": i.url, "username": i.username,
         "description": i.description, "current": i.name == current}
        for i in instances.values()
    ]


@mcp.tool()
async def use_instance(name: str) -> str:
    """Switch the default instance for the rest of this session."""
    global _session_default
    instances, _ = load_instances()
    if name not in instances:
        raise ConfigError(f"Unknown instance '{name}'. Known: {', '.join(instances)}")
    _session_default = name
    return f"Now using '{name}' ({instances[name].url})"


@mcp.tool()
async def add_instance(name: str = "") -> dict:
    """Open a dialog on the user's desktop to add (or re-enter credentials for) an instance.
    The user types URL, username and password there; the password goes straight to the OS
    credential store and is never returned. Call this whenever the user wants to add an
    instance; never ask for passwords in chat. Waits up to 10 minutes for the user."""
    result = await _open_instance_dialog(name)
    if result.get("saved"):
        # Checks REST access and switches to OAuth if the instance restricts Basic auth.
        result.update(await auth.ensure_api_auth(result["name"]))
        _clients.pop(result["name"], None)
    return result


@mcp.tool()
async def keepalive_status(run_now: bool = False) -> dict:
    """Show the PDI keep-alive schedule and recent results (ok / HIBERNATING / LOGIN FAILED).
    run_now=true pings every PDI immediately. Install/remove the schedule with the CLI:
    `servicenow-mcp install-keepalive [--every MIN]` / `uninstall-keepalive`."""
    info = keepalive.task_info()
    schedule = None
    if info:
        fields = dict(line.split(":", 1) for line in info.splitlines() if ":" in line)
        schedule = {k.strip(): v.strip() for k, v in fields.items()
                    if any(w in k for w in ("Status", "Next Run", "状态", "下次运行"))} or info.strip()[:300]
    ran = await keepalive.run_once() if run_now else None
    return {"scheduled": bool(info), "schedule": schedule,
            "pdi_instances": [i.name for i in keepalive.pdi_instances()],
            **({"just_ran": ran} if ran else {}),
            "recent": keepalive.recent(20)}


@mcp.tool()
async def remove_instance(name: str) -> str:
    """Remove an instance from the registry and delete its stored password."""
    global _session_default
    instances, _ = load_instances()
    if name not in instances:
        raise ConfigError(f"Unknown instance '{name}'. Known: {', '.join(instances)}")
    config.remove_instance(name)
    client = _clients.pop(name, None)
    if client:
        await client.close()
    if _session_default == name:
        _session_default = None
    return f"Removed '{name}'"


# ------------------------------------------------------------------ schema

@mcp.tool()
async def describe_table(table: str, include_inherited: bool = True,
                         instance: str | None = None) -> dict:
    """Show a table's label, inheritance chain and fields (type, reference target, mandatory, length,
    default). Use before creating/updating records or writing GlideRecord code."""
    c = await _client(instance)
    chain: list[dict] = []
    name = table
    while name:
        rows = await c.table_get(
            "sys_db_object", sysparm_query=f"name={name}",
            sysparm_fields="name,label,super_class.name,sys_scope.scope,is_extendable",
            sysparm_limit=1,
        )
        if not rows:
            if not chain:
                raise ServiceNowError(f"Table '{table}' does not exist on {c.instance.name}")
            break
        row = rows[0]
        chain.append(row)
        name = row.get("super_class.name") if include_inherited else None

    tables = [t["name"] for t in chain]
    fields = await c.table_get_all(
        "sys_dictionary",
        f"nameIN{','.join(tables)}^elementISNOTEMPTY^ORDERBYelement",
        "name,element,column_label,internal_type,reference,mandatory,max_length,"
        "default_value,read_only,active",
    )
    return {
        "table": table,
        "label": chain[0]["label"],
        "scope": chain[0].get("sys_scope.scope"),
        "extends": tables[1:],
        "fields": [
            {
                "name": f["element"],
                "label": f["column_label"],
                "type": f["internal_type"],
                **({"reference": f["reference"]} if f.get("reference") else {}),
                **({"mandatory": True} if f.get("mandatory") == "true" else {}),
                **({"read_only": True} if f.get("read_only") == "true" else {}),
                **({"inactive": True} if f.get("active") == "false" else {}),
                **({"max_length": f["max_length"]} if f.get("max_length") not in ("", "40", None) else {}),
                **({"default": _short(f["default_value"], 120)} if f.get("default_value") else {}),
                **({"defined_on": f["name"]} if f["name"] != table else {}),
            }
            for f in fields
        ],
    }


# ----------------------------------------------------------------- records

@mcp.tool()
async def query_records(table: str, query: str = "", fields: list[str] | None = None,
                        limit: int = 20, offset: int = 0, order_by: str | None = None,
                        descending: bool = False, display: Display = "both",
                        instance: str | None = None) -> dict:
    """Query any table with an encoded query. `display`: 'both' returns {value, display} for
    reference/choice fields, 'value' raw values only, 'display' display values only.
    Specify `fields` whenever possible to keep output small."""
    c = await _client(instance)
    q = query
    if order_by:
        q = f"{q}^{'ORDERBYDESC' if descending else 'ORDERBY'}{order_by}".lstrip("^")
    rows = await c.table_get(
        table, sysparm_query=q or None, sysparm_fields=_fields(fields),
        sysparm_limit=limit, sysparm_offset=offset,
        sysparm_display_value=_display_param(display), sysparm_exclude_reference_link="true",
    )
    records = [_compact(r) for r in rows] if display == "both" else rows
    return {"count": len(records), "offset": offset, "has_more": len(records) == limit,
            "records": records}


@mcp.tool()
async def get_record(table: str, sys_id: str, fields: list[str] | None = None,
                     display: Display = "both", instance: str | None = None) -> dict:
    """Fetch a single record by sys_id."""
    c = await _client(instance)
    data = await c.request("GET", f"/api/now/table/{table}/{sys_id}", params={
        "sysparm_fields": _fields(fields) or "",
        "sysparm_display_value": _display_param(display),
        "sysparm_exclude_reference_link": "true",
    })
    rec = data["result"]
    return _compact(rec) if display == "both" else rec


@mcp.tool()
async def create_record(table: str, data: dict[str, Any], instance: str | None = None) -> dict:
    """Create a record. Use real field names (see describe_table); reference fields take sys_ids.
    Include `sys_id` in data to force a specific sys_id (useful to keep instances aligned).
    The change is captured in your current update set if the table is tracked."""
    c = await _client(instance)
    res = await c.request("POST", f"/api/now/table/{table}", json=data,
                          params={"sysparm_exclude_reference_link": "true"})
    rec = res["result"]
    return {"sys_id": rec["sys_id"], "table": table,
            "record": {k: _short(v) for k, v in rec.items() if v not in ("", None)}}


@mcp.tool()
async def update_record(table: str, sys_id: str, data: dict[str, Any],
                        instance: str | None = None) -> dict:
    """Update fields on a record (only the given fields change)."""
    c = await _client(instance)
    res = await c.request("PATCH", f"/api/now/table/{table}/{sys_id}", json=data,
                          params={"sysparm_exclude_reference_link": "true",
                                  "sysparm_fields": ",".join(["sys_id", *data.keys()])})
    return {"sys_id": sys_id, "updated": res["result"]}


@mcp.tool()
async def delete_record(table: str, sys_id: str, instance: str | None = None) -> str:
    """Delete a record by sys_id. Irreversible on the instance (unless captured in an update set)."""
    c = await _client(instance)
    await c.request("DELETE", f"/api/now/table/{table}/{sys_id}")
    return f"Deleted {table}/{sys_id} on {c.instance.name}"


@mcp.tool()
async def aggregate(table: str, query: str = "", group_by: list[str] | None = None,
                    count: bool = True, avg: list[str] | None = None, sum: list[str] | None = None,
                    min: list[str] | None = None, max: list[str] | None = None,
                    instance: str | None = None) -> Any:
    """Count / group / avg / sum / min / max over a table (Aggregate API)."""
    c = await _client(instance)
    params = {"sysparm_query": query, "sysparm_count": str(count).lower(),
              "sysparm_display_value": "true"}
    for key, val in (("sysparm_group_by", group_by), ("sysparm_avg_fields", avg),
                     ("sysparm_sum_fields", sum), ("sysparm_min_fields", min),
                     ("sysparm_max_fields", max)):
        if val:
            params[key] = ",".join(val)
    data = await c.request("GET", f"/api/now/stats/{table}", params=params)
    return data["result"]


# -------------------------------------------------------------- dev / debug

@mcp.tool()
async def run_script(script: str, scope: str = "global", instance: str | None = None) -> str:
    """Run a server-side script in Scripts - Background and return its output (gs.print / gs.info
    lines, errors). Full Glide API available. Changes to tracked records go into the current
    update set. Requires admin. `scope` is the application scope name, e.g. 'x_acme_app'."""
    c = await _client(instance)
    out = await c.run_background_script(script, await _scope_sys_id(c, scope))
    return out or "(no output)"


async def _scope_sys_id(c: SNClient, scope: str) -> str:
    """Scripts - Background expects the scope's sys_id; accept a scope name too."""
    if scope == "global" or re.fullmatch(r"[0-9a-f]{32}", scope):
        return scope
    rows = await c.table_get("sys_scope", sysparm_query=f"scope={scope}",
                             sysparm_fields="sys_id", sysparm_limit=1)
    if not rows:
        raise ServiceNowError(f"Application scope '{scope}' not found on {c.instance.name}")
    return rows[0]["sys_id"]


SCRIPT_TABLES: dict[str, list[str]] = {
    "sys_script_include": ["script"],
    "sys_script": ["script"],                       # business rules
    "sys_script_client": ["script"],
    "catalog_script_client": ["script"],
    "sys_ui_action": ["script", "condition"],
    "sys_ui_policy": ["script_true", "script_false"],
    "sys_ui_script": ["script"],
    "sys_ui_page": ["html", "client_script", "processing_script"],
    "sys_ws_operation": ["operation_script"],
    "sysauto_script": ["script"],
    "sys_script_fix": ["script"],
    "sysevent_script_action": ["script"],
    "sys_transform_map": ["script"],
    "sys_transform_script": ["script"],
    "sp_widget": ["script", "client_script", "template"],
}


@mcp.tool()
async def search_scripts(text: str, tables: list[str] | None = None, limit_per_table: int = 20,
                         instance: str | None = None) -> list[dict]:
    """Find code containing `text` across script tables (script includes, business rules,
    client scripts, UI actions/policies/pages, scripted REST, scheduled jobs, fix scripts,
    transform maps, widgets...). Returns matching lines with line numbers."""
    c = await _client(instance)
    targets = {t: SCRIPT_TABLES.get(t, ["script"]) for t in (tables or SCRIPT_TABLES)}

    async def search(table: str, cols: list[str]) -> list[dict]:
        q = "^OR".join(f"{col}LIKE{text}" for col in cols)
        try:
            rows = await c.table_get(
                table, sysparm_query=q, sysparm_limit=limit_per_table,
                sysparm_fields=",".join(["sys_id", "name", "sys_name", "sys_scope.scope",
                                         "active", "collection", *cols]),
            )
        except ServiceNowError:
            return []  # table not present on this instance / plugin not active
        hits = []
        for r in rows:
            lines = []
            for col in cols:
                for no, line in enumerate((r.get(col) or "").splitlines(), 1):
                    if text.lower() in line.lower():
                        lines.append(f"{col}:{no}: {line.strip()[:200]}")
            hits.append({
                "table": table, "sys_id": r["sys_id"],
                "name": r.get("name") or r.get("sys_name"),
                **({"on_table": r["collection"]} if r.get("collection") else {}),
                "scope": r.get("sys_scope.scope"), "active": r.get("active"),
                "matches": lines[:8],
            })
        return hits

    results = await asyncio.gather(*(search(t, cols) for t, cols in targets.items()))
    return [hit for group in results for hit in group]


@mcp.tool()
async def get_logs(minutes: int = 15, level: Literal["error", "warning", "info", "debug"] | None = None,
                   source: str | None = None, contains: str | None = None, limit: int = 50,
                   instance: str | None = None) -> list[dict]:
    """Read recent system logs (syslog), newest first. `level` is the minimum severity."""
    c = await _client(instance)
    q = [f"sys_created_on>=javascript:gs.minutesAgoStart({minutes})"]
    if level:
        q.append("level>=" + {"debug": "-1", "info": "0", "warning": "1", "error": "2"}[level])
    if source:
        q.append(f"sourceLIKE{source}")
    if contains:
        q.append(f"messageLIKE{contains}")
    q.append("ORDERBYDESCsys_created_on")
    rows = await c.table_get("syslog", sysparm_query="^".join(q), sysparm_limit=limit,
                             sysparm_fields="sys_created_on,level,source,message",
                             sysparm_display_value="true")
    return [{**r, "message": _short(r.get("message"), 1000)} for r in rows]


@mcp.tool()
async def check_access(user: str, table: str, sys_id: str | None = None,
                       operations: list[Literal["read", "write", "create", "delete"]] | None = None,
                       fields: list[str] | None = None, instance: str | None = None) -> dict:
    """Explain record access for a user: impersonates `user` (user_name, email, name or sys_id),
    checks read/write/create/delete on `table` (a specific record via sys_id, or a new record),
    and lists every relevant record ACL (table hierarchy, table.*, table.field, *) with whether
    its roles, condition and script pass. `fields` adds field-level checks. Read-only."""
    c = await _client(instance)
    return await debugtools.check_access(c, user, table, sys_id,
                                         operations or ["read", "write", "create", "delete"], fields)


@mcp.tool()
async def flow_executions(flow: str | None = None, record: str | None = None, table: str | None = None,
                          state: Literal["IN_PROGRESS", "WAITING", "COMPLETE", "ERROR", "CANCELLED"] | None = None,
                          hours: int = 24, limit: int = 10, include_test_runs: bool = False,
                          instance: str | None = None) -> dict:
    """Recent Flow Designer executions, newest first: state, errors and flow logs. Filter by flow
    name (contains), by source `record` (number like RITM0010001 or sys_id; pass `table` for
    non-task records) and/or state. With a record it also lists its approvals."""
    c = await _client(instance)
    return await debugtools.flow_executions(c, flow, record, table, state, hours, limit, include_test_runs)


@mcp.tool()
async def email_trace(record: str, table: str | None = None, hours: int = 72,
                      instance: str | None = None) -> dict:
    """Trace notifications for a record (number or sys_id; pass `table` for non-task records):
    events fired, emails generated (type, recipients, errors, which notification), every active
    notification on the table with its trigger and whether its condition matches the record now,
    plus instance mail settings that commonly explain 'no email was sent'."""
    c = await _client(instance)
    return await debugtools.email_trace(c, record, table, hours)


@mcp.tool()
async def run_atf(test: str | None = None, suite: str | None = None, wait_seconds: int = 300,
                  instance: str | None = None) -> dict:
    """Run an ATF test or every active test of a suite (name or sys_id) and return per-step
    results. Tests with UI steps need a client test runner open in a browser."""
    c = await _client(instance)
    return await debugtools.run_atf(c, test, suite, wait_seconds)


UI_PATHS = {
    "home": "/now/nav/ui/home",
    "atf_runner": "/atf_test_runner.do?sysparm_nostack=true",
    "scripts_background": "/sys.scripts.do",
    "update_sets": "/sys_update_set_list.do?sysparm_query=state%3Din%20progress",
    "system_logs": "/syslog_list.do?sysparm_query=ORDERBYDESCsys_created_on",
}


@mcp.tool()
async def ui_link(kind: Literal["form", "list", "new", "flow", "flow_execution", "page"] = "form",
                  table: str | None = None, sys_id: str | None = None, query: str | None = None,
                  view: str | None = None, page: str | None = None,
                  instance: str | None = None) -> dict:
    """Exact URL of an instance page, for opening in the browser (Claude in Chrome) or giving to
    the user. form: table+sys_id; list: table (+query); new: table; flow: sys_id of the flow
    (sys_hub_flow); flow_execution: sys_id of the flow context; page: one of home, atf_runner,
    scripts_background, update_sets, system_logs, or a raw path like '/sp'."""
    from urllib.parse import quote
    c = await _client(instance)
    base = c.instance.url
    v = f"&sysparm_view={quote(view)}" if view else ""
    if kind in ("form", "list", "new") and not table:
        raise ServiceNowError(f"`table` is required for kind='{kind}'")
    if kind == "form":
        if not sys_id:
            raise ServiceNowError("`sys_id` is required for a form; use kind='new' for a new record")
        path = f"/{table}.do?sys_id={sys_id}{v}"
    elif kind == "new":
        path = f"/{table}.do?sys_id=-1{v}"
    elif kind == "list":
        path = f"/{table}_list.do" + (f"?sysparm_query={quote(query)}" if query else "") + (v.replace("&", "?", 1) if v and not query else v)
    elif kind == "flow":
        path = f"/$flow-designer.do?sysparm_nostack=true#/flow-designer/{sys_id}"
    elif kind == "flow_execution":
        path = f"/now/workflow-studio/builder?tableName=sys_flow_context&builderId=flow-execution&sysId={sys_id}"
    else:
        path = UI_PATHS.get(page or "home", page or "/")
        if not path.startswith("/"):
            path = "/" + path
    return {"url": base + path, "instance": c.instance.name,
            "note": "Browser must be logged in to this instance; if a login page shows, ask the user to log in."}


# ------------------------------------------------------------- update sets

async def _current_update_set(c: SNClient) -> dict:
    """The user's current application and the update set selected for it."""
    user_id = await _my_user_sys_id(c)
    prefs = await c.table_get(
        "sys_user_preference",
        sysparm_query=f"user={user_id}^nameINapps.current_app,sys_update_set^ORnameSTARTSWITHupdateSetForScope",
        sysparm_fields="name,value",
    )
    by_name = {p["name"]: p["value"] for p in prefs}
    app = by_name.get("apps.current_app") or "global"
    us = by_name.get(f"updateSetForScope{app}") or by_name.get("sys_update_set")
    return {"application": app, "update_set": us}


@mcp.tool()
async def list_update_sets(state: str = "in progress", limit: int = 30,
                           instance: str | None = None) -> dict:
    """List update sets (default: in progress) and show your current application + update set."""
    c = await _client(instance)
    current = await _current_update_set(c)
    q = (f"state={state}^" if state else "") + "ORDERBYDESCsys_updated_on"
    rows = await c.table_get("sys_update_set", sysparm_query=q, sysparm_limit=limit,
                             sysparm_fields="sys_id,name,state,application.scope,description,"
                                            "sys_updated_on,sys_created_by")
    return {"current_application": current["application"],
            "current_update_set": current["update_set"],
            "update_sets": [{**r, "current": r["sys_id"] == current["update_set"]} for r in rows]}


@mcp.tool()
async def set_current_update_set(name_or_sys_id: str, create_if_missing: bool = False,
                                 application: str | None = None, description: str = "",
                                 instance: str | None = None) -> dict:
    """Make an in-progress update set current, and switch your current application to the update
    set's application, so that subsequent REST/script changes are captured in it.
    With create_if_missing, creates it in `application` (scope name, e.g. 'global' or
    'x_acme_app'; default: your current application)."""
    c = await _client(instance)
    rows = await c.table_get(
        "sys_update_set",
        sysparm_query=f"sys_id={name_or_sys_id}^ORname={name_or_sys_id}^state=in progress",
        sysparm_fields="sys_id,name,application,application.scope", sysparm_limit=5,
    )
    if application:
        app_id = await _scope_sys_id(c, application)
        rows = [r for r in rows if r["application"] == app_id]
    if len(rows) > 1:
        raise ServiceNowError(
            f"'{name_or_sys_id}' matches update sets in several applications: "
            f"{', '.join(r['application.scope'] for r in rows)}. Pass `application`."
        )
    if not rows:
        if not create_if_missing:
            raise ServiceNowError(f"No in-progress update set named '{name_or_sys_id}'. "
                                  "Pass create_if_missing=true to create it.")
    # Writing sys_user_preference directly is not enough: the instance caches preferences.
    # GlideUpdateSet.set() updates the session, the preference and the cache.
    # New update sets are created inside the script, after switching the application, because
    # the platform assigns the current application to new update sets regardless of input.
    if rows:
        us = {"sys_id": rows[0]["sys_id"], "name": rows[0]["name"]}
        app = rows[0]["application"]
        create = ""
    else:
        if not create_if_missing:
            raise ServiceNowError(f"No in-progress update set named '{name_or_sys_id}'. "
                                  "Pass create_if_missing=true to create it.")
        app = await _scope_sys_id(c, application) if application else (await _current_update_set(c))["application"]
        us = {"sys_id": None, "name": name_or_sys_id}
        create = (
            "var g = new GlideRecord('sys_update_set'); g.initialize();"
            f"g.name = {json.dumps(name_or_sys_id)}; g.description = {json.dumps(description)};"
            f"g.application = '{app}'; g.state = 'in progress'; usId = g.insert();"
        )
    out = await c.run_background_script(
        # setCurrentApplicationId only affects this session; REST sessions read the preference.
        f"gs.setCurrentApplicationId('{app}'); gs.getUser().savePreference('apps.current_app', '{app}');"
        f"var usId = '{us['sys_id'] or ''}';"
        + create +
        "var u = new GlideUpdateSet(); u.set(usId);"
        "gs.print('APP=' + gs.getCurrentApplicationScope() + ' US=' + u.get() + ' WANT=' + usId);"
    )
    m = re.search(r"APP=(\S+) US=(\w+) WANT=(\w+)", out)
    if m:
        us["sys_id"] = m.group(3)
    m = re.search(r"APP=(\S+) US=(\w+)", out)
    if not m or m.group(2) != us["sys_id"]:
        raise ServiceNowError(f"Failed to switch update set. Script output:\n{out}")
    return {"current_update_set": us["name"], "sys_id": us["sys_id"],
            "current_application": m.group(1), "instance": c.instance.name}


@mcp.tool()
async def get_update_set_changes(update_set: str, limit: int = 200,
                                 instance: str | None = None) -> dict:
    """List the customer updates (sys_update_xml) captured in an update set (name or sys_id)."""
    c = await _client(instance)
    sets = await c.table_get("sys_update_set",
                             sysparm_query=f"sys_id={update_set}^ORname={update_set}",
                             sysparm_fields="sys_id,name,state", sysparm_limit=1)
    if not sets:
        raise ServiceNowError(f"Update set '{update_set}' not found")
    rows = await c.table_get("sys_update_xml",
                             sysparm_query=f"update_set={sets[0]['sys_id']}^ORDERBYtype",
                             sysparm_fields="type,target_name,name,action,sys_updated_on,sys_updated_by",
                             sysparm_limit=limit)
    return {"update_set": sets[0], "count": len(rows), "changes": rows}


@mcp.tool()
async def migrate_update_set(update_set: str, source: str, target: str, commit: bool = False,
                             replace_existing: bool = False, wait_seconds: int = 300) -> dict:
    """Move an update set (name or sys_id) from `source` to `target` instance: load it as a
    retrieved (remote) update set on the target, run Preview, and report preview problems.
    With commit=true it also commits, but only if preview found no unresolved problems.
    Otherwise review the problems, then use resolve_preview_problems and commit_update_set.
    Source should normally be 'complete'. Batch (parent/child) update sets are not supported."""
    src, tgt = await _client(source), await _client(target)
    us = await updateset.find_update_set(src, update_set)
    warnings = []
    if us["state"] != "complete":
        warnings.append(f"Source update set is '{us['state']}', not 'complete'.")
    remote_id, count = await updateset.load(src, tgt, us, replace_existing)
    prev = await updateset.preview(tgt, remote_id, wait_seconds)
    probs = await updateset.problems(tgt, remote_id)
    unresolved = [p for p in probs if p["status"] == "unresolved"]
    result = {
        "update_set": us["name"], "application": us.get("application.scope"),
        "source": source, "target": target, "updates_loaded": count,
        "remote_update_set": remote_id, "preview": prev,
        "problems": probs, "unresolved_problems": len(unresolved),
        **({"warnings": warnings} if warnings else {}),
    }
    if commit and prev["state"] == "successful" and not unresolved:
        result["commit"] = await updateset.commit(tgt, remote_id, wait_seconds)
    elif commit:
        result["commit"] = "skipped: preview not successful or unresolved problems remain"
    return result


@mcp.tool()
async def resolve_preview_problems(remote_update_set: str, action: Literal["accept", "skip"],
                                   problem_ids: list[str] | None = None,
                                   instance: str | None = None) -> dict:
    """Resolve preview problems of a retrieved update set, like the UI buttons:
    'accept' = Accept remote update (apply it anyway), 'skip' = Skip remote update (don't apply).
    Without problem_ids, applies to all unresolved problems. Returns the remaining problems."""
    c = await _client(instance)
    done = await updateset.resolve(c, remote_update_set, action, problem_ids)
    probs = await updateset.problems(c, remote_update_set)
    return {**done, "problems": probs,
            "unresolved_problems": sum(p["status"] == "unresolved" for p in probs)}


@mcp.tool()
async def commit_update_set(remote_update_set: str, wait_seconds: int = 300,
                            instance: str | None = None) -> dict:
    """Commit a previewed retrieved update set (sys_remote_update_set sys_id) on the instance.
    Refuses while preview problems are unresolved."""
    c = await _client(instance)
    return await updateset.commit(c, remote_update_set, wait_seconds)


# --------------------------------------------------------- cross-instance

def _diff(a: dict, b: dict, fields: set[str] | None, ignore: set[str]) -> dict:
    keys = (fields or (set(a) | set(b))) - ignore
    return {k: {"source": _short(a.get(k), 200), "target": _short(b.get(k), 200)}
            for k in sorted(keys) if (a.get(k) or "") != (b.get(k) or "")}


@mcp.tool()
async def compare_records(table: str, query: str, source: str, target: str,
                          fields: list[str] | None = None, match_on: str = "sys_id",
                          ignore_fields: list[str] | None = None, max_records: int = 2000) -> dict:
    """Diff records between two instances (e.g. dev vs test). Matches by `match_on` (sys_id by
    default; use e.g. 'name' if the records were created separately). Reports records missing on
    either side and field-level differences. System audit fields are ignored."""
    src, tgt = await _client(source), await _client(target)
    field_str = _fields(fields)
    if field_str:
        field_str = ",".join({*field_str.split(","), "sys_id", match_on})
    a_rows, b_rows = await asyncio.gather(
        src.table_get_all(table, query, field_str, max_records=max_records),
        tgt.table_get_all(table, query, field_str, max_records=max_records),
    )
    a = {r.get(match_on): r for r in a_rows}
    b = {r.get(match_on): r for r in b_rows}
    ignore = SYSTEM_FIELDS | set(ignore_fields or [])
    if match_on != "sys_id":
        ignore.add("sys_id")

    def label(r: dict) -> str:
        return r.get("name") or r.get("sys_name") or r.get("number") or r.get(match_on) or r["sys_id"]

    different = []
    for key in a.keys() & b.keys():
        d = _diff(a[key], b[key], set(fields) if fields else None, ignore)
        if d:
            different.append({"key": key, "name": label(a[key]), "diff": d})
    return {
        "table": table, "source": source, "target": target,
        "source_count": len(a), "target_count": len(b),
        "only_in_source": [{"key": k, "name": label(a[k])} for k in a.keys() - b.keys()],
        "only_in_target": [{"key": k, "name": label(b[k])} for k in b.keys() - a.keys()],
        "different": different,
        "identical": len(a.keys() & b.keys()) - len(different),
    }


@mcp.tool()
async def copy_records(table: str, query: str, source: str, target: str,
                       fields: list[str] | None = None, dry_run: bool = True,
                       max_records: int = 200) -> dict:
    """Copy records from one instance to another, keeping the same sys_id (insert if missing,
    update if different). Defaults to dry_run=true: review the plan, then call again with
    dry_run=false. Referenced records (e.g. groups, users) must already exist on the target.
    Writes on the target are captured in the target user's current update set if tracked."""
    src, tgt = await _client(source), await _client(target)
    src_rows = await src.table_get_all(table, query, _fields(fields), max_records=max_records + 1)
    if len(src_rows) > max_records:
        raise ServiceNowError(f"Query matches more than {max_records} records; narrow it or raise max_records.")
    ids = [r["sys_id"] for r in src_rows]
    tgt_rows: list[dict] = []
    for i in range(0, len(ids), 100):
        tgt_rows += await tgt.table_get_all(table, f"sys_idIN{','.join(ids[i:i + 100])}", _fields(fields))
    existing = {r["sys_id"]: r for r in tgt_rows}

    plan = {"create": [], "update": [], "unchanged": 0}
    for r in src_rows:
        payload = {k: v for k, v in r.items() if k not in SYSTEM_FIELDS}
        if r["sys_id"] not in existing:
            plan["create"].append(payload)
        else:
            changed = {k: v for k, v in payload.items() if (existing[r["sys_id"]].get(k) or "") != (v or "")}
            if changed:
                plan["update"].append({"sys_id": r["sys_id"], **changed})
            else:
                plan["unchanged"] += 1

    def name(p: dict) -> str:
        return p.get("name") or p.get("sys_name") or p.get("number") or p["sys_id"]

    summary = {
        "table": table, "source": source, "target": target, "dry_run": dry_run,
        "to_create": [name(p) for p in plan["create"]],
        "to_update": [{"name": name(p), "fields": [k for k in p if k != "sys_id"]} for p in plan["update"]],
        "unchanged": plan["unchanged"],
    }
    if dry_run:
        return summary

    failures = []
    for p in plan["create"]:
        try:
            await tgt.request("POST", f"/api/now/table/{table}", json=p,
                              params={"sysparm_fields": "sys_id"})
        except ServiceNowError as e:
            failures.append({"name": name(p), "action": "create", "error": str(e)})
    for p in plan["update"]:
        sid = p.pop("sys_id")
        try:
            await tgt.request("PATCH", f"/api/now/table/{table}/{sid}", json=p,
                              params={"sysparm_fields": "sys_id"})
        except ServiceNowError as e:
            failures.append({"name": name({**p, "sys_id": sid}), "action": "update", "error": str(e)})
    return {**summary, "failures": failures}


# ------------------------------------------------------------- escape hatch

@mcp.tool()
async def rest_request(method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"], path: str,
                       params: dict[str, str] | None = None, body: Any = None,
                       instance: str | None = None) -> Any:
    """Call any REST endpoint on the instance (path like '/api/now/attachment' or a scripted REST
    API '/api/x_scope/my_api/...'). Use when no dedicated tool fits."""
    c = await _client(instance)
    if not path.startswith("/"):
        path = "/" + path
    return await c.request(method, path, params=params, json=body)


def run() -> None:
    mcp.run()
