"""Choose how REST calls authenticate. OAuth is preferred; Basic auth is the fallback.

ServiceNow restricts Basic auth for APIs to users with the `snc_basic_auth_api_access` role
(glide.authenticate.basic_auth.restriction.*) on newer instances, while UI login keeps working.
We register an OAuth client on the instance through the UI session and use the OAuth password
grant. No instance settings or roles are changed.
"""

from __future__ import annotations

import secrets

import httpx

from . import config
from .client import ServiceNowError, SNClient

OAUTH_APP_NAME = "servicenow-mcp"
# Marks the provisioning script so its execution-history entry (which contains the secret) can be
# found and deleted. The cleanup script builds the marker by concatenation so it never matches itself.
MARKER = "servicenow-mcp:oauth-provision"


async def _basic_rest_ok(inst: config.Instance) -> int:
    async with httpx.AsyncClient(base_url=inst.url, timeout=30) as h:
        r = await h.get("/api/now/table/sys_user", auth=(inst.username, inst.password),
                        params={"sysparm_limit": 1, "sysparm_fields": "sys_id"},
                        headers={"Accept": "application/json"})
    return r.status_code


async def provision_oauth(inst: config.Instance) -> str:
    """Create (or rotate the secret of) the OAuth client on the instance. Returns client_id."""
    client = SNClient(inst)
    secret = secrets.token_urlsafe(32)
    try:
        out = await client.run_background_script(f"""// {MARKER}
var e = new GlideRecord('oauth_entity');
e.addQuery('name', '{OAUTH_APP_NAME}'); e.addQuery('type', 'client'); e.query();
var isNew = !e.next();
if (isNew) {{
  e.initialize(); e.name = '{OAUTH_APP_NAME}'; e.type = 'client';
  e.comments = 'API access for servicenow-mcp (Claude Code). Safe to delete; it will be recreated when needed.';
}}
e.client_secret = '{secret}'; e.active = true;
if (isNew) e.insert(); else e.update();
gs.print('MCPOAUTH client_id=' + e.client_id);""")
        client_id = next((ln.split("client_id=", 1)[1].strip()
                          for ln in out.splitlines() if "MCPOAUTH client_id=" in ln), "")
        # Remove the execution-history entry that holds the script (and so the secret).
        await client.run_background_script(
            "var h = new GlideRecord('sys_script_execution_history');"
            f"h.addQuery('script', 'CONTAINS', '{MARKER[:20]}' + '{MARKER[20:]}');"
            "h.query(); while (h.next()) h.deleteRecord();"
        )
    finally:
        await client.close()
    if not client_id:
        raise ServiceNowError(f"[{inst.name}] Could not register the OAuth client:\n{out[-800:]}")
    config.set_oauth(inst.name, client_id, secret)
    return client_id


async def _rest_ok(inst: config.Instance) -> str | None:
    """None if a REST call works with the instance's current auth, else the error text."""
    c = SNClient(inst)
    try:
        await c.table_get("sys_user", sysparm_limit=1, sysparm_fields="sys_id")
        return None
    except (ServiceNowError, config.ConfigError) as e:
        return str(e)
    finally:
        await c.close()


async def ensure_api_auth(name: str) -> dict:
    """Make REST access work for an instance, preferring OAuth.

    OAuth (password grant with a registered client) is the default because ServiceNow is phasing
    out Basic auth for APIs. Basic auth is used only when OAuth cannot be set up, e.g. the UI login
    is unavailable (SSO-only) or the password grant is disabled on the instance.
    """
    inst = config.load_instances()[0][name]
    if inst.auth == "oauth" and await _rest_ok(inst) is None:
        return {"auth": "oauth", "rest_ok": True}

    # Registering the OAuth client needs a UI session; this also proves the password is right.
    probe = SNClient(inst)
    try:
        await probe.run_background_script("gs.print('ok')")
        ui_ok, ui_error = True, ""
    except ServiceNowError as e:
        ui_ok, ui_error = False, str(e)
    finally:
        await probe.close()

    oauth_error = "UI login failed, so the OAuth client could not be registered"
    if ui_ok:
        try:
            await provision_oauth(inst)
            oauth_error = await _rest_ok(config.load_instances()[0][name])
        except (ServiceNowError, config.ConfigError) as e:
            oauth_error = str(e)
        if oauth_error is None:
            return {"auth": "oauth", "rest_ok": True, "oauth_client": OAUTH_APP_NAME}
        config.set_basic(name)  # OAuth did not work: fall back below

    basic = config.load_instances()[0][name]
    status = await _basic_rest_ok(basic)
    if status == 200:
        return {"auth": "basic", "rest_ok": True,
                "note": f"OAuth unavailable, using Basic auth. Reason: {oauth_error}"}
    if not ui_ok:
        return {"auth": "basic", "rest_ok": False,
                "error": f"Login failed: wrong username/password or account locked. ({ui_error})"}
    return {"auth": "basic", "rest_ok": False,
            "error": f"OAuth failed ({oauth_error}) and Basic auth is rejected (HTTP {status}). "
                     "Enable the OAuth password grant or grant the snc_basic_auth_api_access role."}
