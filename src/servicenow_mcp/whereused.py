"""Where-used search and change-history search.

where_used: find everywhere a name (a table, field, script include, etc.) is referenced -
in code and conditions (text search) and in metadata (dictionary reference/dependent fields,
UI policy / data policy conditions, email notification conditions, ACLs, reference qualifiers).

change_history: query sys_update_xml - what changed, in which application / update set / by whom
over a time window, the audit trail behind "what was touched recently".
"""

from __future__ import annotations

import asyncio

from .client import ServiceNowError, SNClient

# Tables whose text columns may mention a name. Superset of SCRIPT_TABLES plus condition holders.
TEXT_SOURCES: dict[str, tuple[list[str], str]] = {
    # table: (columns to search, label field)
    "sys_script_include": (["script"], "name"),
    "sys_script": (["script", "condition", "filter_condition"], "name"),
    "sys_script_client": (["script"], "name"),
    "catalog_script_client": (["script"], "name"),
    "sys_ui_action": (["script", "condition"], "name"),
    "sys_ui_policy": (["script_true", "script_false", "conditions"], "short_description"),
    "sys_ui_policy_action": (["mandatory", "visible"], "field"),
    "sys_ui_script": (["script"], "name"),
    "sys_ui_page": (["html", "client_script", "processing_script"], "name"),
    "sys_ws_operation": (["operation_script"], "name"),
    "sysauto_script": (["script", "condition"], "name"),
    "sys_script_fix": (["script"], "name"),
    "sysevent_script_action": (["script", "condition"], "name"),
    "sys_transform_map": (["script"], "name"),
    "sys_transform_script": (["script"], "name"),
    "sp_widget": (["script", "client_script", "template", "link", "demo_data", "option_schema"], "name"),
    "sysevent_email_action": (["condition", "advanced_condition", "message", "message_html",
                               "subject", "recipient_fields"], "name"),
    "sys_security_acl": (["script", "condition"], "name"),
    "sys_data_policy2": (["conditions"], "short_description"),
    "sys_dictionary": (["reference_qual", "calculation", "default_value", "dynamic_creation_script",
                        "function_definition"], "element"),
    "wf_workflow_version": (["condition"], "name"),
    "sla_definition": (["start_condition", "stop_condition", "pause_condition", "reset_condition"], "name"),
}

# Metadata references keyed on a name: dictionary columns that *point at* a table or field.
# query is built per kind in where_used().


async def where_used(c: SNClient, name: str, kind: str, limit_per_source: int,
                     include_inactive: bool) -> dict:
    hits: dict[str, list] = {"code": [], "metadata": []}

    active = "" if include_inactive else "^active=true"

    async def search_text(table: str, cols: list[str], label: str) -> list[dict]:
        q = "^OR".join(f"{col}LIKE{name}" for col in cols)
        try:
            rows = await c.table_get(
                table, sysparm_query=q + (active if _has_active(table) else ""),
                sysparm_limit=limit_per_source,
                sysparm_fields=",".join(["sys_id", label, "sys_scope.scope",
                                         "sys_updated_on", *cols, *(["collection"] if table in COLLECTION_TABLES else []),
                                         *(["name"] if table == "sys_dictionary" else [])]),
            )
        except ServiceNowError:
            return []
        out = []
        for r in rows:
            where = []
            for col in cols:
                for no, line in enumerate((r.get(col) or "").splitlines(), 1):
                    if name.lower() in line.lower():
                        where.append(f"{col}:{no}: {line.strip()[:160]}")
            out.append({
                "table": table, "sys_id": r["sys_id"],
                "label": r.get(label) or r.get("name") or r["sys_id"],
                **({"on_table": r["collection"]} if r.get("collection") else {}),
                **({"on_table": r["name"]} if table == "sys_dictionary" and r.get("name") else {}),
                "scope": r.get("sys_scope.scope"), "updated": r.get("sys_updated_on"),
                "matches": where[:6],
            })
        return [o for o in out if o["matches"]]

    results = await asyncio.gather(*(search_text(t, cols, label)
                                     for t, (cols, label) in TEXT_SOURCES.items()))
    hits["code"] = [h for group in results for h in group]

    # Metadata references to a table: fields whose reference / dependent / choice table is this name.
    if kind in ("table", "auto"):
        meta_q = f"reference={name}^ORdependent_on_field={name}^ORchoice_table={name}"
        refs = await c.table_get("sys_dictionary", sysparm_query=meta_q,
                                 sysparm_fields="name,element,internal_type,reference,sys_scope.scope",
                                 sysparm_limit=limit_per_source * 2)
        # Verify (the Table API coerces types loosely): keep only rows that truly point at `name`.
        hits["metadata"] += [{
            "kind": "reference field", "table": r["name"], "field": r["element"],
            "type": r["internal_type"], "scope": r.get("sys_scope.scope"),
            "detail": f"{r['name']}.{r['element']} ({r['internal_type']}) -> {name}",
        } for r in refs if r.get("reference") == name or not r.get("reference")]

    # Tables that extend this table.
    if kind in ("table", "auto"):
        children = await c.table_get("sys_db_object", sysparm_query=f"super_class.name={name}",
                                     sysparm_fields="name,label,sys_scope.scope", sysparm_limit=limit_per_source)
        hits["metadata"] += [{"kind": "child table", "table": r["name"], "label": r["label"],
                              "scope": r.get("sys_scope.scope")} for r in children]

    total = len(hits["code"]) + len(hits["metadata"])
    by_table: dict[str, int] = {}
    for h in hits["code"]:
        by_table[h["table"]] = by_table.get(h["table"], 0) + 1
    return {"name": name, "kind": kind, "total": total,
            "code_references": len(hits["code"]), "metadata_references": len(hits["metadata"]),
            "by_table": dict(sorted(by_table.items(), key=lambda x: -x[1])),
            "code": hits["code"], "metadata": hits["metadata"],
            "note": ("Searched code/condition text and dictionary metadata. Text search is substring and "
                     "case-insensitive, so review matches for false positives (a name inside a longer name).")}


COLLECTION_TABLES = {"sys_script", "sys_ui_action", "sys_ui_policy", "sys_ui_policy_action",
                     "sys_script_client", "sys_data_policy2", "sla_definition", "sys_security_acl"}
_ACTIVE_TABLES = {"sys_script_include", "sys_script", "sys_script_client", "sys_ui_action",
                  "sys_ui_policy", "sys_ui_script", "sysauto_script", "sysevent_email_action",
                  "sys_security_acl", "sys_data_policy2", "catalog_script_client", "sla_definition"}


def _has_active(table: str) -> bool:
    return table in _ACTIVE_TABLES


# --------------------------------------------------------------- change history

TYPE_HINTS = {
    "business rule": "Business Rule", "script include": "Script Include",
    "client script": "Client Script", "ui action": "UI Action", "ui policy": "UI Policy",
    "acl": "ACL", "flow": "Flow", "table": "Table", "dictionary": "Dictionary",
    "notification": "Email Notification", "property": "System Property",
}


async def change_history(c: SNClient, since_hours: int, application: str | None, updated_by: str | None,
                         type_contains: str | None, target_contains: str | None, update_set: str | None,
                         limit: int) -> dict:
    q = [f"sys_updated_on>=javascript:gs.hoursAgoStart({since_hours})"]
    if application:
        app = await _scope_sys_id(c, application)
        q.append(f"application={app}")
    if updated_by:
        q.append(f"sys_updated_byLIKE{updated_by}")
    if type_contains:
        q.append(f"typeLIKE{TYPE_HINTS.get(type_contains.lower(), type_contains)}")
    if target_contains:
        q.append(f"target_nameLIKE{target_contains}")
    if update_set:
        us = await c.table_get("sys_update_set", sysparm_query=f"sys_id={update_set}^ORname={update_set}",
                               sysparm_fields="sys_id,name", sysparm_limit=1)
        if not us:
            raise ServiceNowError(f"Update set '{update_set}' not found")
        q.append(f"update_set={us[0]['sys_id']}")
    q.append("ORDERBYDESCsys_updated_on")

    rows = await c.table_get("sys_update_xml", sysparm_query="^".join(q), sysparm_limit=limit,
                             sysparm_fields="type,target_name,action,sys_updated_on,sys_updated_by,"
                                            "application.scope,update_set.name,update_set.state")
    changes = [{
        "type": r["type"], "target": r["target_name"], "action": r["action"],
        "updated": r["sys_updated_on"], "by": r["sys_updated_by"],
        "scope": r.get("application.scope"),
        "update_set": r.get("update_set.name"), "update_set_state": r.get("update_set.state"),
    } for r in rows]
    by_type: dict[str, int] = {}
    by_user: dict[str, int] = {}
    for ch in changes:
        by_type[ch["type"]] = by_type.get(ch["type"], 0) + 1
        by_user[ch["by"]] = by_user.get(ch["by"], 0) + 1
    return {"since_hours": since_hours, "count": len(changes),
            "has_more": len(changes) == limit,
            "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
            "by_user": dict(sorted(by_user.items(), key=lambda x: -x[1])),
            "changes": changes}


async def _scope_sys_id(c: SNClient, scope: str) -> str:
    import re
    if re.fullmatch(r"[0-9a-f]{32}", scope):
        return scope
    rows = await c.table_get("sys_scope", sysparm_query=f"scope={scope}",
                             sysparm_fields="sys_id", sysparm_limit=1)
    if not rows:
        raise ServiceNowError(f"Application scope '{scope}' not found on {c.instance.name}")
    return rows[0]["sys_id"]
