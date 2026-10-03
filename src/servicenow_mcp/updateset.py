"""Update set migration between instances.

Uses the same steps as the UI:
  1. 'Export to XML' on the source (UpdateSetExport + export_update_set.do), then
     'Import Update Set from XML' on the target (sys_upload.do). sys_update_xml cannot be
     inserted over REST (ACL), so the XML route is the supported one,
  2. preview with the same worker the UI uses (UpdateSetPreviewer),
  3. commit with the same logic as UpdateSetCommitAjax.commitRemoteUpdateSet.
Workers run in the background on the instance; we poll sys_execution_tracker.
"""

from __future__ import annotations

import asyncio
import re
import time

from .client import ServiceNowError, SNClient

TRACKER_STATES = {"0": "pending", "1": "running", "2": "successful", "3": "failed", "4": "cancelled"}


def _sid(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value or ""):
        raise ServiceNowError(f"Not a sys_id: {value!r}")
    return value


async def _script(c: SNClient, script: str, marker: str) -> dict[str, str]:
    """Run a background script that prints `MARKER key=value ...` and parse that line."""
    out = await c.run_background_script(script)
    m = re.search(rf"{marker} (.*)", out)
    if not m:
        raise ServiceNowError(f"[{c.instance.name}] Script did not complete:\n{out[-1500:]}")
    return dict(re.findall(r"(\w+)=(\S*)", m.group(1)))


async def wait_tracker(c: SNClient, tracker_id: str, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        rows = await c.table_get("sys_execution_tracker", sysparm_query=f"sys_id={tracker_id}",
                                 sysparm_fields="state,message,percent_complete,result",
                                 sysparm_limit=1)
        t = rows[0] if rows else {"state": "0"}
        state = TRACKER_STATES.get(t.get("state"), t.get("state"))
        if state in ("successful", "failed", "cancelled"):
            if state == "failed" and '"has_preview_problem":true' in (t.get("result") or ""):
                state = "completed_with_problems"  # the platform marks a preview with problems as failed
            return {"state": state, "message": t.get("message")}
        if time.monotonic() > deadline:
            return {"state": f"still {state} after {timeout}s", "percent": t.get("percent_complete"),
                    "message": t.get("message")}
        await asyncio.sleep(2)


async def find_update_set(c: SNClient, name_or_sys_id: str) -> dict:
    rows = await c.table_get(
        "sys_update_set",
        sysparm_query=f"sys_id={name_or_sys_id}^ORname={name_or_sys_id}^ORDERBYDESCsys_updated_on",
        sysparm_fields="sys_id,name,description,state,release_date,application,parent,"
                       "application.scope,application.name,application.version",
        sysparm_limit=5,
    )
    if not rows:
        raise ServiceNowError(f"[{c.instance.name}] Update set '{name_or_sys_id}' not found")
    if len(rows) > 1:
        listing = ", ".join(f"{r['sys_id']} ({r['application.scope']}, {r['state']})" for r in rows)
        raise ServiceNowError(f"Several update sets are named '{name_or_sys_id}': {listing}. Pass the sys_id.")
    return rows[0]


async def delete_remote_update_set(c: SNClient, remote_id: str) -> None:
    await _script(c, f"""
var id = '{_sid(remote_id)}';
new UpdateSetPreviewer().removePreviewRecords(id);
var x = new GlideRecord('sys_update_xml'); x.addQuery('remote_update_set', id); x.deleteMultiple();
var r = new GlideRecord('sys_remote_update_set'); if (r.get(id)) r.deleteRecord();
gs.print('MCPDONE ok=1');""", "MCPDONE")


async def load(src: SNClient, tgt: SNClient, us: dict, replace_existing: bool) -> tuple[str, int]:
    """Copy update set `us` from src into a loaded sys_remote_update_set on tgt."""
    children = await src.table_get("sys_update_set", sysparm_query=f"parent={us['sys_id']}",
                                   sysparm_fields="sys_id", sysparm_limit=1)
    if children or us.get("parent"):
        raise ServiceNowError("Batch (parent/child) update sets are not supported yet; migrate a single set.")

    existing = await tgt.table_get(
        "sys_remote_update_set", sysparm_query=f"remote_sys_id={us['sys_id']}",
        sysparm_fields="sys_id,state,sys_updated_on")
    for r in existing:
        if r["state"] == "committed":
            if not replace_existing:
                raise ServiceNowError(
                    f"This update set was already committed on {tgt.instance.name} "
                    f"({r['sys_updated_on']}). Pass replace_existing=true to load it again.")
            continue  # keep history of committed ones
        await delete_remote_update_set(tgt, r["sys_id"])

    count = await src.table_get("sys_update_xml", sysparm_query=f"update_set={us['sys_id']}",
                                sysparm_fields="sys_id", sysparm_limit=1)
    if not count:
        raise ServiceNowError(f"Update set '{us['name']}' has no changes to migrate")

    # Export on the source exactly like the 'Export to XML' UI action...
    info = await _script(src, f"""
var us = new GlideRecord('sys_update_set');
if (!us.get('{_sid(us["sys_id"])}')) {{ gs.print('MCPDONE error=not_found'); }}
else {{ gs.print('MCPDONE remote=' + new UpdateSetExport().exportUpdateSet(us)); }}""", "MCPDONE")
    if "remote" not in info:
        raise ServiceNowError(f"Export of '{us['name']}' failed on {src.instance.name}")
    remote_id = info["remote"]
    xml = await src.export_remote_update_set_xml(remote_id)

    # ...and import on the target like 'Import Update Set from XML'. The sys_id is preserved.
    await tgt.import_update_set_xml(f"sys_remote_update_set_{remote_id}.xml", xml)
    loaded = await tgt.table_get("sys_remote_update_set", sysparm_query=f"sys_id={remote_id}",
                                 sysparm_fields="sys_id,state", sysparm_limit=1)
    if not loaded:
        raise ServiceNowError(f"Upload finished but the update set did not appear on {tgt.instance.name}")
    stats = await tgt.request("GET", "/api/now/stats/sys_update_xml",
                              params={"sysparm_query": f"remote_update_set={remote_id}",
                                      "sysparm_count": "true"})
    return remote_id, int(stats["result"]["stats"]["count"])


async def preview(c: SNClient, remote_id: str, timeout: int) -> dict:
    info = await _script(c, f"""
var r = new GlideRecord('sys_remote_update_set');
if (!r.get('{_sid(remote_id)}')) {{ gs.print('MCPDONE error=not_found'); }} else {{
  r.state = 'previewing'; r.update();
  var w = new GlideScriptedHierarchicalWorker();
  w.setProgressName('Generating Update Set Preview for: ' + r.name);
  w.setScriptIncludeName('UpdateSetPreviewer');
  w.setScriptIncludeMethod('generatePreviewRecordsWithUpdate');
  w.putMethodArg('sys_id', r.sys_id);
  w.setSource(r.sys_id); w.setSourceTable('sys_remote_update_set');
  w.setBackground(true); w.setCannotCancel(true); w.start();
  gs.print('MCPDONE tracker=' + w.getProgressID());
}}""", "MCPDONE")
    if "tracker" not in info:
        raise ServiceNowError(f"Remote update set {remote_id} not found on {c.instance.name}")
    return await wait_tracker(c, info["tracker"], timeout)


async def problems(c: SNClient, remote_id: str) -> list[dict]:
    rows = await c.table_get_all(
        "sys_update_preview_problem", f"remote_update_set={remote_id}^ORDERBYtype",
        "sys_id,type,status,description,missing_item_table,missing_item,remote_update.name,"
        "remote_update.target_name", max_records=1000)
    return [{
        "sys_id": r["sys_id"], "type": r["type"], "status": r.get("status") or "unresolved",
        "record": r.get("remote_update.target_name") or r.get("remote_update.name"),
        "description": r["description"],
        **({"missing": f"{r['missing_item_table']}:{r['missing_item']}"}
           if r.get("missing_item_table") else {}),
    } for r in rows]


async def resolve(c: SNClient, remote_id: str, action: str, problem_ids: list[str] | None) -> dict:
    method = {"accept": "ignoreProblem", "skip": "skipUpdate"}[action]
    ids = ",".join(_sid(p) for p in problem_ids) if problem_ids else ""
    return await _script(c, f"""
var p = new GlideRecord('sys_update_preview_problem');
p.addQuery('remote_update_set', '{_sid(remote_id)}');
{"p.addQuery('sys_id', 'IN', '" + ids + "');" if ids else "p.addNullQuery('status');"}
p.query();
var n = 0, failed = 0;
while (p.next()) {{
  var id = p.getUniqueValue();
  // The action may throw afterwards (it tries to redirect the missing UI); judge by the result.
  try {{ new GlidePreviewProblemAction(null, p).{method}(); }} catch (e) {{}}
  var chk = new GlideRecord('sys_update_preview_problem');
  if (chk.get(id) && !chk.status.nil()) n++; else failed++;
}}
gs.print('MCPDONE resolved=' + n + ' failed=' + failed);""", "MCPDONE")


async def commit(c: SNClient, remote_id: str, timeout: int) -> dict:
    rows = await c.table_get("sys_remote_update_set", sysparm_query=f"sys_id={_sid(remote_id)}",
                             sysparm_fields="state,name", sysparm_limit=1)
    if not rows:
        raise ServiceNowError(f"Remote update set {remote_id} not found on {c.instance.name}")
    if rows[0]["state"] != "previewed":
        raise ServiceNowError(f"Remote update set is '{rows[0]['state']}', it must be 'previewed' to commit.")
    unresolved = [p for p in await problems(c, remote_id) if p["status"] == "unresolved"]
    if unresolved:
        raise ServiceNowError(f"{len(unresolved)} preview problems are unresolved. "
                              "Resolve them (accept / skip) before committing.")

    # Same steps as UpdateSetCommitAjax.commitRemoteUpdateSet.
    info = await _script(c, f"""
var r = new GlideRecord('sys_remote_update_set'); r.get('{remote_id}');
var w = new GlideUpdateSetWorker();
var lus = new GlideRecord('sys_update_set');
var lid = w.remoteUpdateSetCommit(lus, r, r.update_source.url);
new UpdateSetCommitAjax()._copyUpdateXML(lid, r.sys_id);
r.update();
w.setUpdateSetSysId(lid);
w.setProgressName('Committing update set: ' + r.name);
w.setBackground(true); w.start();
gs.print('MCPDONE tracker=' + w.getProgressID() + ' local=' + lid);""", "MCPDONE")
    result = await wait_tracker(c, info["tracker"], timeout)
    final = await c.table_get("sys_remote_update_set", sysparm_query=f"sys_id={remote_id}",
                              sysparm_fields="state", sysparm_limit=1)
    return {"commit": result, "remote_state": final[0]["state"] if final else None,
            "local_update_set": info.get("local")}
