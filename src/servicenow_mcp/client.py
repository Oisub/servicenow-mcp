"""HTTP client for one ServiceNow instance.

- REST calls use basic auth, or an OAuth bearer token (password grant) for instances that
  restrict Basic auth, with retries on transient failures.
- UI-only features (background scripts) use a separate form-login session.
- Every failure is turned into a ServiceNowError with a message that says what to do.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from typing import Any

import httpx

from .config import Instance

RETRY_STATUS = {429, 502, 503, 504}
MAX_RETRIES = 3
TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class ServiceNowError(Exception):
    pass


def _explain(resp: httpx.Response, instance: Instance) -> str:
    detail = ""
    try:
        err = resp.json().get("error", {})
        detail = " - ".join(x for x in (err.get("message"), err.get("detail")) if x)
    except Exception:
        detail = resp.text[:300]
    hint = {
        400: "Bad request: check the encoded query, field names and value formats.",
        401: f"Authentication failed for '{instance.username}'. Check the password or whether the "
             "account is locked; if the instance restricts Basic auth, run add_instance again "
             "(it sets up OAuth automatically).",
        403: "Forbidden by ACL or the user lacks the required role for this table/operation.",
        404: "Not found: the table, record or endpoint does not exist on this instance.",
    }.get(resp.status_code, "")
    return f"[{instance.name}] HTTP {resp.status_code}: {detail or resp.reason_phrase}. {hint}".strip()


def _looks_hibernating(resp: httpx.Response) -> bool:
    ctype = resp.headers.get("content-type", "")
    return "text/html" in ctype and ("hibernat" in resp.text.lower() or "instance is" in resp.text.lower())


class SNClient:
    def __init__(self, instance: Instance):
        self.instance = instance
        self._oauth = instance.auth == "oauth"
        self._http = httpx.AsyncClient(
            base_url=instance.url,
            auth=None if self._oauth else (instance.username, instance.password),
            timeout=TIMEOUT,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        self._ui: httpx.AsyncClient | None = None
        self._ui_lock = asyncio.Lock()
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()
        if self._ui:
            await self._ui.aclose()

    # ----------------------------------------------------------------- OAuth

    async def _bearer(self, force: bool = False) -> str:
        async with self._token_lock:
            if self._token and not force and time.monotonic() < self._token_expires:
                return self._token
            resp = await self._http.post("/oauth_token.do", data={
                "grant_type": "password",
                "client_id": self.instance.client_id,
                "client_secret": self.instance.client_secret,
                "username": self.instance.username,
                "password": self.instance.password,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code != 200 or "access_token" not in body:
                raise ServiceNowError(
                    f"[{self.instance.name}] OAuth token request failed (HTTP {resp.status_code}: "
                    f"{body.get('error_description') or body.get('error') or resp.text[:150]}). "
                    "If the password changed or the OAuth client was removed, run add_instance again."
                )
            self._token = body["access_token"]
            self._token_expires = time.monotonic() + int(body.get("expires_in", 1800)) - 60
            return self._token

    # ------------------------------------------------------------------ REST

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json: Any = None) -> Any:
        last_exc: Exception | None = None
        renewed = False
        for attempt in range(MAX_RETRIES):
            # Stateless REST: a reused session would keep a stale current update set / application
            # after they are switched, so changes would be captured in the wrong update set.
            self._http.cookies.clear()
            headers = {"Authorization": f"Bearer {await self._bearer()}"} if self._oauth else None
            try:
                resp = await self._http.request(method, path, params=params, json=json, headers=headers)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last_exc = e
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code == 401 and self._oauth and not renewed:
                renewed = True  # token revoked or expired early: get a fresh one once
                await self._bearer(force=True)
                continue
            if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES - 1:
                await asyncio.sleep(float(resp.headers.get("Retry-After", 2 ** attempt)))
                continue
            if _looks_hibernating(resp):
                raise ServiceNowError(
                    f"[{self.instance.name}] The instance appears to be hibernating or unavailable. "
                    "Wake it at https://developer.servicenow.com and retry."
                )
            if resp.status_code >= 400:
                raise ServiceNowError(_explain(resp, self.instance))
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                raise ServiceNowError(
                    f"[{self.instance.name}] Expected JSON but got {resp.headers.get('content-type')}: "
                    f"{resp.text[:200]}"
                )
        raise ServiceNowError(f"[{self.instance.name}] Could not reach {self.instance.url}: {last_exc}")

    async def table_get(self, table: str, **params) -> list[dict]:
        params = {"sysparm_exclude_reference_link": "true",
                  **{k: v for k, v in params.items() if v is not None}}
        data = await self.request("GET", f"/api/now/table/{table}", params=params)
        return (data or {}).get("result", [])

    async def table_get_all(self, table: str, query: str, fields: str | None,
                            page_size: int = 500, max_records: int = 5000) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while len(out) < max_records:
            batch = await self.table_get(
                table, sysparm_query=query, sysparm_fields=fields,
                sysparm_limit=page_size, sysparm_offset=offset,
                sysparm_exclude_reference_link="true",
            )
            out.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return out[:max_records]

    # -------------------------------------------------------------- UI session

    async def _login(self) -> httpx.AsyncClient:
        ui = httpx.AsyncClient(base_url=self.instance.url, timeout=TIMEOUT, follow_redirects=True)
        resp = await ui.post("/login.do", data={
            "user_name": self.instance.username,
            "user_password": self.instance.password,
            "sys_action": "sysverb_login",
        })
        if resp.status_code >= 400:
            await ui.aclose()
            raise ServiceNowError(f"[{self.instance.name}] UI login failed (HTTP {resp.status_code}).")
        return ui

    async def _csrf_token(self, ui: httpx.AsyncClient) -> str | None:
        page = await ui.get("/sys.scripts.do")
        m = re.search(r"""name=["']sysparm_ck["'][^>]*value=["']([^"']+)""", page.text) or \
            re.search(r"""value=["']([^"']+)["'][^>]*name=["']sysparm_ck["']""", page.text)
        return m.group(1) if m else None

    async def reset_ui_session(self) -> None:
        async with self._ui_lock:
            if self._ui:
                await self._ui.aclose()
            self._ui = None

    async def _ui_session(self) -> httpx.AsyncClient:
        if self._ui is None:
            self._ui = await self._login()
        return self._ui

    async def export_remote_update_set_xml(self, remote_id: str) -> bytes:
        """Download a sys_remote_update_set as XML (what 'Export to XML' does) and delete the copy."""
        async with self._ui_lock:
            for attempt in range(2):
                ui = await self._ui_session()
                ck = await self._csrf_token(ui)  # the processor rejects requests without it (401)
                if ck:
                    break
                await ui.aclose()
                self._ui = None
            else:
                raise ServiceNowError(f"[{self.instance.name}] UI login failed; cannot export.")
            resp = await ui.get("/export_update_set.do",
                                params={"sysparm_sys_id": remote_id, "sysparm_delete_when_done": "true",
                                        "sysparm_ck": ck})
        if resp.status_code >= 400 or b"<unload" not in resp.content[:500]:
            raise ServiceNowError(f"[{self.instance.name}] Update set export failed "
                                  f"(HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.content

    async def import_update_set_xml(self, filename: str, content: bytes) -> None:
        """Upload an update set XML like 'Import Update Set from XML' in the UI."""
        referring = "sys_remote_update_set_list.do"
        async with self._ui_lock:
            for attempt in range(2):
                ui = await self._ui_session()
                page = await ui.get("/upload.do", params={"sysparm_referring_url": referring,
                                                          "sysparm_target": "sys_remote_update_set"})
                m = re.search(r"""name=["']sysparm_ck["'][^>]*value=["']([^"']+)""", page.text)
                if m:
                    break
                await ui.aclose()
                self._ui = None
            else:
                raise ServiceNowError(f"[{self.instance.name}] Could not open the XML upload page.")
            resp = await ui.post("/sys_upload.do", data={
                "sysparm_ck": m.group(1),
                "sysparm_upload_prefix": "",
                "sysparm_referring_url": referring,
                "sysparm_target": "sys_remote_update_set",
            }, files={"attachFile": (filename, content, "text/xml")})
        if resp.status_code >= 400:
            raise ServiceNowError(f"[{self.instance.name}] XML upload failed: HTTP {resp.status_code}")

    async def run_background_script(self, script: str, scope: str = "global") -> str:
        async with self._ui_lock:
            for attempt in range(2):
                if self._ui is None:
                    self._ui = await self._login()
                ck = await self._csrf_token(self._ui)
                if not ck:
                    # Session expired or login page returned: re-login once.
                    await self._ui.aclose()
                    self._ui = None
                    if attempt == 0:
                        continue
                    raise ServiceNowError(
                        f"[{self.instance.name}] Could not open Scripts - Background. "
                        "Check the password (UI login failed) and that the user has the admin role."
                    )
                resp = await self._ui.post("/sys.scripts.do", data={
                    "script": script,
                    "sysparm_ck": ck,
                    "runscript": "Run script",
                    "sys_scope": scope,
                    "quota_managed_transaction": "on",
                })
                if resp.status_code >= 400:
                    raise ServiceNowError(f"[{self.instance.name}] Script run failed: HTTP {resp.status_code}")
                return _extract_script_output(resp.text)
        raise ServiceNowError("unreachable")


def _extract_script_output(page: str) -> str:
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", page)
    body = re.sub(r"(?i)<br\s*/?>", "\n", body)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", body))
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and ln not in ("Script execution history", "available here")]
    # Drop Java stack frames from script errors; the error message itself is kept.
    lines = [ln for ln in lines if not JAVA_FRAME.match(ln) and not SQL_DEBUG.match(ln)]
    return "\n".join(lines)


SQL_DEBUG = re.compile(r"^Time: \d+:\d+:\d+\.\d+ id: .* for: ")  # session SQL debug output
JAVA_FRAME = re.compile(r"^(com|org|java|jdk|sun)\.[\w.$/]+\(.*\)$")
