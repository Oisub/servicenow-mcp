"""Debugging tools that need server-side Glide APIs: ACL analysis, flow executions,
notification/email tracing and ATF runs. Each runs a background script that prints one
`MCPJSON {...}` line, which is parsed here.
"""

from __future__ import annotations

import json
import re

from .client import ServiceNowError, SNClient
from .updateset import wait_tracker


async def script_json(c: SNClient, body: str, args: dict) -> dict:
    """Run `body` with `ARGS` defined; the script must set `out` (an object)."""
    script = (f"var ARGS = {json.dumps(args)}; var out = {{}};\n"
              f"try {{\n{body}\n}} catch (e) {{ out = {{error: String(e)}}; }}\n"
              "gs.print('MCPJSON ' + JSON.stringify(out));")
    text = await c.run_background_script(script)
    m = re.search(r"MCPJSON (\{.*\})\s*$", text, re.M)
    if not m:
        raise ServiceNowError(f"[{c.instance.name}] Script failed:\n{text[-1500:]}")
    out = json.loads(m.group(1))
    if "error" in out and len(out) == 1:
        raise ServiceNowError(f"[{c.instance.name}] {out['error']}")
    return out


# Shared JS helpers: resolve a user and a record from what a person would type.
JS_HELPERS = r"""
function findUser(key) {
  var u = new GlideRecord('sys_user');
  if (/^[0-9a-f]{32}$/.test(key) && u.get(key)) return u;
  u = new GlideRecord('sys_user'); if (u.get('user_name', key)) return u;
  u = new GlideRecord('sys_user'); if (u.get('email', key)) return u;
  u = new GlideRecord('sys_user'); u.addQuery('name', key); u.query(); if (u.getRowCount() == 1 && u.next()) return u;
  throw 'User not found (or ambiguous): ' + key;
}
function findRecord(table, key) {
  var t = table || 'task';
  var gr = new GlideRecord(t);
  if (!gr.isValid()) throw 'Table not found: ' + t;
  if (/^[0-9a-f]{32}$/.test(key)) { if (gr.get(key)) return reclass(gr); }
  else if (gr.isValidField('number')) { gr = new GlideRecord(t); if (gr.get('number', key)) return reclass(gr); }
  throw 'Record not found in ' + t + ': ' + key + (table ? '' : ' (pass `table` for non-task records)');
}
function reclass(gr) {  // a task found via 'task' -> reload as its real class (incident, sc_req_item...)
  var cls = gr.getValue('sys_class_name');
  if (cls && cls != gr.getTableName()) { var r = new GlideRecord(cls); if (r.get(gr.getUniqueValue())) return r; }
  return gr;
}
function hoursAgo(h) { var d = new GlideDateTime(); d.addSeconds(-3600 * h); return d.getValue(); }
"""


# ------------------------------------------------------------------ ACLs

CHECK_ACCESS_JS = JS_HELPERS + r"""
var u = findUser(ARGS.user);
var gr = new GlideRecord(ARGS.table);
if (!gr.isValid()) throw 'Table not found: ' + ARGS.table;
if (ARGS.sys_id) { if (!gr.get(ARGS.sys_id)) throw 'Record not found: ' + ARGS.table + '/' + ARGS.sys_id; }
else gr.initialize();

var tables = []; var tu = new TableUtils(gr.getTableName()).getTables();
for (var i = 0; i < tu.size(); i++) tables.push(String(tu.get(i)));
var names = ['*', '*.*'];
tables.forEach(function (t) {
  names.push(t, t + '.*');
  (ARGS.fields || []).forEach(function (f) { names.push(t + '.' + f); });
});

var acls = [];
var a = new GlideRecord('sys_security_acl');
a.addQuery('name', 'IN', names.join(','));
a.addQuery('operation.name', 'IN', ARGS.operations.join(','));
a.addQuery('type.name', 'record');
a.addActiveQuery();
a.orderBy('name'); a.query();
while (a.next()) {
  var roles = []; var r = new GlideRecord('sys_security_acl_role');
  r.addQuery('sys_security_acl', a.getUniqueValue()); r.query();
  while (r.next()) roles.push(r.sys_user_role.name + '');
  acls.push({ sys_id: a.getUniqueValue(), name: a.name + '', operation: a.operation.name + '',
    decision: (a.decision_type + '') || 'allow', admin_overrides: a.admin_overrides + '' == 'true',
    roles: roles, condition: a.condition + '', script: a.advanced + '' == 'true' ? (a.script + '') : '' ,
    security_attribute: a.security_attribute.getDisplayValue() || '', description: a.description + '' });
}

out = { user: { sys_id: u.getUniqueValue(), user_name: u.user_name + '', name: u.name + '', active: u.active + '' },
        table: gr.getTableName(), record: ARGS.sys_id ? (gr.getDisplayValue() || ARGS.sys_id) : '(new record)',
        tables: tables, operations: {}, fields: {}, acls: acls };

var origUser = gs.getUserID();
var imp = new GlideImpersonate();
try {
  imp.impersonate(u.getUniqueValue());
  var myRoles = String(gs.getUser().getRoles()).replace(/[\[\]\s]/g, '').split(',').filter(String);
  out.user.roles = myRoles;
  var isAdmin = gs.hasRole('admin');
  var g2 = new GlideRecord(gr.getTableName());
  if (ARGS.sys_id) g2.get(ARGS.sys_id); else g2.initialize();
  ARGS.operations.forEach(function (op) {
    var m = { read: 'canRead', write: 'canWrite', create: 'canCreate', 'delete': 'canDelete' }[op];
    out.operations[op] = m ? g2[m]() : 'n/a';
  });
  (ARGS.fields || []).forEach(function (f) {
    if (!g2.isValidField(f)) { out.fields[f] = 'no such field'; return; }
    out.fields[f] = { read: g2[f].canRead(), write: g2[f].canWrite() };
  });
  acls.forEach(function (acl) {
    var roleOk = acl.roles.length == 0 || acl.roles.some(function (x) { return myRoles.indexOf(x) > -1; });
    var condOk = true, scriptOk = true, scriptErr = '';
    if (acl.condition) condOk = GlideFilter.checkRecord(g2, acl.condition);
    if (acl.script) {
      try {
        var aclGr = new GlideRecord('sys_security_acl'); aclGr.get(acl.sys_id);
        var ev = new GlideScopedEvaluator(); ev.putVariable('current', g2); ev.putVariable('answer', null);
        var res = ev.evaluateScript(aclGr, 'script');
        var ans = ev.getVariable('answer'); if (ans === null || ans === undefined) ans = res;
        scriptOk = ans === true || String(ans) == 'true';
      } catch (e) { scriptOk = false; scriptErr = String(e); }
    }
    var met = (roleOk && condOk && scriptOk) || (isAdmin && acl.admin_overrides);
    acl.result = { roles: roleOk, condition: condOk, script: scriptOk, criteria_met: met,
                   effect: acl.decision == 'deny' ? (met ? 'does not deny' : 'DENIES') : (met ? 'GRANTS' : 'does not grant') };
    if (acl.security_attribute) acl.result.not_evaluated = 'security attribute ' + acl.security_attribute;
    if (scriptErr) acl.result.script_error = scriptErr;
    if (acl.script.length > 400) acl.script = acl.script.substring(0, 400) + '...';
  });
} finally {
  imp.impersonate(origUser);
}
out.restored = gs.getUserID() == origUser;
"""


async def check_access(c: SNClient, user: str, table: str, sys_id: str | None,
                       operations: list[str], fields: list[str] | None) -> dict:
    out = await script_json(c, CHECK_ACCESS_JS, {
        "user": user, "table": table, "sys_id": sys_id or "",
        "operations": operations, "fields": fields or [],
    })
    if not out.pop("restored", True):
        await c.reset_ui_session()  # never keep working as the impersonated user
    # Most specific ACL first, as the platform evaluates table.field before table.* before table.
    out["acls"].sort(key=lambda a: (a["operation"], -a["name"].count("."), a["name"] == "*"))
    def failed(a: dict) -> list[str]:
        return [k for k in ("roles", "condition", "script") if not a["result"][k]]

    out["summary"] = {}
    for op in operations:
        mine = [a for a in out["acls"] if a["operation"] == op]
        out["summary"][op] = {
            "allowed": out["operations"].get(op),  # the platform's own decision (authoritative)
            "table_level_grant": any(a["result"]["effect"] == "GRANTS" and "." not in a["name"] for a in mine),
            "granted_by": [a["name"] for a in mine if a["result"]["effect"] == "GRANTS"],
            "denied_by": [{"name": a["name"], "failed": failed(a)} for a in mine if a["result"]["effect"] == "DENIES"],
            "not_granting": [{"name": a["name"], "failed": failed(a)}
                             for a in mine if a["result"]["effect"] == "does not grant"],
        }
        if any("not_evaluated" in a["result"] for a in mine):
            out["summary"][op]["note"] = "Some ACLs use security attributes, which are not evaluated here."
    out["how_to_read"] = ("Access needs a granting allow-ACL at table level (and field level for fields) and no "
                          "denying deny-ACL. 'allowed' is the platform's real answer; per-ACL results explain it. "
                          "Script results can differ from the platform when a script relies on internal "
                          "variables (e.g. root_rule).")
    return out


# ----------------------------------------------------------------- flows

FLOW_JS = JS_HELPERS + r"""
var ctx = new GlideRecord('sys_flow_context');
ctx.addQuery('sys_created_on', '>=', hoursAgo(ARGS.hours));
var rec = null;
if (ARGS.record) { rec = findRecord(ARGS.table, ARGS.record); ctx.addQuery('source_record', rec.getUniqueValue()); }
if (ARGS.flow) ctx.addQuery('name', 'CONTAINS', ARGS.flow);
if (ARGS.state) ctx.addQuery('state', ARGS.state);
if (!ARGS.include_tests) ctx.addQuery('is_test_run', false);
ctx.orderByDesc('sys_created_on'); ctx.setLimit(ARGS.limit); ctx.query();
var list = [];
while (ctx.next()) {
  var e = { sys_id: ctx.getUniqueValue(), name: ctx.name + '', state: ctx.state + '',
            started: ctx.sys_created_on.getDisplayValue(), run_time_ms: ctx.run_time + '',
            source: ctx.source_table + ':' + ctx.source_record, flow_id: ctx.flow + '' };
  if (ctx.error_state + '') e.error_state = ctx.error_state + '';
  if (ctx.error_message + '') e.error = ctx.error_message + '';
  var lg = new GlideRecord('sys_flow_log'); lg.addQuery('context', ctx.getUniqueValue()); lg.orderBy('order'); lg.setLimit(30); lg.query();
  var lvl = { '0': 'INFO', '1': 'WARN', '2': 'ERROR' };
  var logs = []; while (lg.next()) logs.push((lvl[lg.level + ''] || lg.level + '') + ' ' + (lg.message + '').substring(0, 400));
  if (logs.length) e.logs = logs;
  list.push(e);
}
out.executions = list;
if (rec) {
  out.record = { table: rec.getTableName(), sys_id: rec.getUniqueValue(), display: rec.getDisplayValue() };
  var ap = new GlideRecord('sysapproval_approver'); ap.addQuery('document_id', rec.getUniqueValue()); ap.orderBy('sys_created_on'); ap.query();
  var aps = []; while (ap.next()) aps.push({ approver: ap.approver.getDisplayValue(), state: ap.state + '',
    created: ap.sys_created_on.getDisplayValue(), updated: ap.sys_updated_on.getDisplayValue(), comments: (ap.comments.getJournalEntry(1) + '').substring(0, 300) });
  out.approvals = aps;
  if (rec.isValidField('approval')) out.record.approval = rec.approval + '';
}
out.reporting_level = gs.getProperty('com.snc.process_flow.reporting.level', 'OFF');
"""


async def flow_executions(c: SNClient, flow: str | None, record: str | None, table: str | None,
                          state: str | None, hours: int, limit: int, include_tests: bool) -> dict:
    out = await script_json(c, FLOW_JS, {
        "flow": flow or "", "record": record or "", "table": table or "", "state": state or "",
        "hours": hours, "limit": limit, "include_tests": include_tests,
    })
    if out.get("reporting_level", "OFF") == "OFF":
        out["note"] = ("Flow reporting is OFF, so per-step inputs/outputs are not recorded. To debug a "
                       "flow step by step, set com.snc.process_flow.reporting.level=TRACE and re-run it.")
    return out


# ------------------------------------------------------- notifications / email

EMAIL_JS = JS_HELPERS + r"""
var rec = findRecord(ARGS.table, ARGS.record);
var id = rec.getUniqueValue(), since = hoursAgo(ARGS.hours);
out.record = { table: rec.getTableName(), sys_id: id, display: rec.getDisplayValue() };
out.system = { smtp_sending_enabled: gs.getProperty('glide.email.smtp.active') + '',
               test_user_redirect: gs.getProperty('glide.email.test.user', '') };

var ev = new GlideRecord('sysevent'); ev.addQuery('instance', id); ev.addQuery('sys_created_on', '>=', since);
ev.orderBy('sys_created_on'); ev.setLimit(50); ev.query();
out.events = []; while (ev.next()) out.events.push({ name: ev.name + '', state: ev.state + '', created: ev.sys_created_on.getDisplayValue(),
  processed: ev.processed.getDisplayValue(), parm1: (ev.parm1 + '').substring(0, 200), parm2: (ev.parm2 + '').substring(0, 200) });

var em = new GlideRecord('sys_email'); em.addQuery('instance', id); em.addQuery('sys_created_on', '>=', since);
em.orderBy('sys_created_on'); em.setLimit(30); em.query();
out.emails = []; while (em.next()) {
  var e = { sys_id: em.getUniqueValue(), type: em.type + '', subject: em.subject + '', recipients: em.recipients + '',
            created: em.sys_created_on.getDisplayValue() };
  if (em.error_string + '') e.error = em.error_string + '';
  var lg = new GlideRecord('sys_email_log');
  if (lg.isValid()) { lg.addQuery('email', em.getUniqueValue()); lg.query();
    if (lg.next()) { e.notification = lg.notification.getDisplayValue(); if (lg.event) e.event = lg.event.getDisplayValue(); } }
  out.emails.push(e);
}

var tables = []; var tu = new TableUtils(rec.getTableName()).getTables();
for (var i = 0; i < tu.size(); i++) tables.push(String(tu.get(i)));
var n = new GlideRecord('sysevent_email_action'); n.addQuery('collection', 'IN', tables.join(',')); n.addActiveQuery();
n.orderBy('name'); n.setLimit(60); n.query();
out.notifications = []; while (n.next()) {
  var trig = n.event_name + '' ? 'event ' + n.event_name : [n.action_insert + '' == 'true' ? 'insert' : '', n.action_update + '' == 'true' ? 'update' : ''].filter(String).join('/');
  var cond = n.condition + '';
  var x = { name: n.name + '', table: n.collection + '', trigger: trig || '(none)', sys_id: n.getUniqueValue(),
            condition: cond ? n.condition.getDisplayValue() : '', condition_matches_now: cond ? GlideFilter.checkRecord(rec, cond) : true,
            recipients: [n.recipient_users.getDisplayValue(), n.recipient_groups.getDisplayValue(), n.recipient_fields + ''].filter(String).join(' | '),
            send_to_event_creator: n.send_self + '' };
  if (n.advanced_condition + '') x.has_advanced_condition = true;
  out.notifications.push(x);
}
"""


async def email_trace(c: SNClient, record: str, table: str | None, hours: int) -> dict:
    out = await script_json(c, EMAIL_JS, {"record": record, "table": table or "", "hours": hours})
    hints = []
    if out["system"]["smtp_sending_enabled"] != "true":
        hints.append("Outbound email is disabled on this instance (glide.email.smtp.active=false): "
                     "emails are generated in sys_email but never actually sent.")
    if out["system"]["test_user_redirect"]:
        hints.append(f"All email is redirected to glide.email.test.user = {out['system']['test_user_redirect']}.")
    if not out["events"] and not out["emails"]:
        hints.append("No events or emails for this record in the time window: check the notification "
                     "trigger (insert/update vs event) and whether the record actually changed.")
    out["hints"] = hints
    return out


# ------------------------------------------------------------------- ATF

ATF_RESOLVE_JS = r"""
if (gs.getProperty('sn_atf.runner.enabled') + '' != 'true')
  throw 'ATF test execution is disabled on this instance (sn_atf.runner.enabled=false). Enable it in Automated Test Framework > Administration > Properties.';
function findBy(table, key) {
  var g = new GlideRecord(table);
  if (/^[0-9a-f]{32}$/.test(key) && g.get(key)) return g;
  g = new GlideRecord(table); g.addQuery('name', key); g.query();
  if (g.getRowCount() == 1 && g.next()) return g;
  throw (g.getRowCount() > 1 ? 'Several ' : 'No ') + table + ' named ' + key;
}
out.tests = [];
if (ARGS.suite) {
  var s = findBy('sys_atf_test_suite', ARGS.suite); out.suite = s.name + '';
  var st = new GlideRecord('sys_atf_test_suite_test'); st.addQuery('test_suite', s.getUniqueValue());
  st.addQuery('test.active', true); st.orderBy('order'); st.query();
  while (st.next()) out.tests.push({ sys_id: st.test + '', name: st.test.name + '' });
} else {
  var t = findBy('sys_atf_test', ARGS.test); out.tests.push({ sys_id: t.getUniqueValue(), name: t.name + '' });
}
"""

ATF_START_JS = r"""
var x = new sn_atf.ExecuteUserTest(); x.setTestRecordSysId(ARGS.test);
out.tracker = String(x.start());
"""

ATF_RESULT_JS = r"""
var r = new GlideRecord('sys_atf_test_result');
r.addQuery('execution_tracker', ARGS.tracker).addOrCondition('root_tracker_id', ARGS.tracker);
r.orderByDesc('sys_created_on'); r.setLimit(1); r.query();
if (!r.next()) { out.status = 'no result yet'; }
else {
  out.status = r.status + ''; out.run_time = r.run_time.getDisplayValue(); out.result_sys_id = r.getUniqueValue();
  if (r.output + '') out.output = (r.output + '').substring(0, 1500);
  var s = new GlideRecord('sys_atf_test_result_step'); s.addQuery('test_result', r.getUniqueValue()); s.orderBy('order'); s.query();
  out.steps = [];
  while (s.next()) {
    var step = { order: s.order + '', step: s.step.getDisplayValue(), status: s.status + '' };
    var msg = (s.isValidField('summary') ? s.summary + '' : '') || (s.isValidField('output') ? s.output + '' : '');
    if (msg && step.status != 'success') step.message = msg.substring(0, 800);
    out.steps.push(step);
  }
}
"""


async def run_atf(c: SNClient, test: str | None, suite: str | None, wait_seconds: int) -> dict:
    if bool(test) == bool(suite):
        raise ServiceNowError("Pass exactly one of `test` or `suite`.")
    plan = await script_json(c, ATF_RESOLVE_JS, {"test": test or "", "suite": suite or ""})
    if not plan["tests"]:
        raise ServiceNowError(f"Suite '{plan.get('suite')}' has no active tests.")
    results = []
    for t in plan["tests"]:
        started = await script_json(c, ATF_START_JS, {"test": t["sys_id"]})
        tracker = await wait_tracker(c, started["tracker"], wait_seconds)
        res = await script_json(c, ATF_RESULT_JS, {"tracker": started["tracker"]})
        entry = {"test": t["name"], "status": res.get("status"), **{k: v for k, v in res.items() if k != "status"}}
        if tracker["state"].startswith("still"):
            entry["note"] = ("Still running. If the test has UI steps it needs a client test runner: open "
                             f"{c.instance.url}/atf_test_runner.do in a browser logged in to the instance.")
        results.append(entry)
    passed = sum(r["status"] == "success" for r in results)
    return {**({"suite": plan["suite"]} if plan.get("suite") else {}),
            "passed": passed, "total": len(results), "results": results}
