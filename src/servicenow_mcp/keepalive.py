"""Keep personal developer instances (PDIs) from hibernating.

A PDI hibernates after a period without developer activity, and waking it needs the developer
portal. So we periodically log in through the UI and open a page, like a developer would.
This cannot wake an instance that is already hibernating; that is reported in the log instead.
Runs from Windows Task Scheduler (`install-keepalive`), so only while this PC is on.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx

from . import config

TASK_NAME = "ServiceNow PDI keepalive"
LOG_FILE = config.CONFIG_DIR / "keepalive.log"
LOG_MAX_LINES = 500
PDI_HOST = re.compile(r"^https://dev\d+\.service-now\.com$")


def pdi_instances() -> list[config.Instance]:
    instances, _ = config.load_instances()
    return [i for i in instances.values() if PDI_HOST.match(i.url)]


async def ping(inst: config.Instance) -> str:
    """Log in through the UI and load a page. Returns a one-line status."""
    try:
        async with httpx.AsyncClient(base_url=inst.url, timeout=60, follow_redirects=True) as ui:
            resp = await ui.post("/login.do", data={
                "user_name": inst.username, "user_password": inst.password,
                "sys_action": "sysverb_login",
            })
            if "hibernat" in resp.text.lower():
                return "HIBERNATING - wake it at https://developer.servicenow.com"
            page = await ui.get("/sys.scripts.do")
            if "sysparm_ck" not in page.text:
                return f"LOGIN FAILED (HTTP {page.status_code})"
            await ui.get("/logout.do")
            return "ok"
    except (httpx.HTTPError, config.ConfigError) as e:
        return f"ERROR {type(e).__name__}: {e}"


def _log(lines: list[str]) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    old = LOG_FILE.read_text(encoding="utf-8").splitlines() if LOG_FILE.exists() else []
    LOG_FILE.write_text("\n".join((old + lines)[-LOG_MAX_LINES:]) + "\n", encoding="utf-8")


async def run_once(names: list[str] | None = None) -> list[str]:
    targets = [i for i in pdi_instances() if not names or i.name in names]
    results = await asyncio.gather(*(ping(i) for i in targets))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"{stamp}  {i.name:16} {r}" for i, r in zip(targets, results)]
    if not targets:
        lines = [f"{stamp}  (no PDI instances configured)"]
    _log(lines)
    return lines


def recent(n: int = 20) -> list[str]:
    if not LOG_FILE.exists():
        return []
    return LOG_FILE.read_text(encoding="utf-8").splitlines()[-n:]


def task_info() -> str | None:
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def install(interval_minutes: int) -> str:
    # pythonw runs without a console window, so nothing flashes on screen.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    cmd = f'"{exe}" -m servicenow_mcp.cli keepalive'
    r = subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "MINUTE",
                        "/MO", str(interval_minutes), "/TR", cmd], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return f"Scheduled '{TASK_NAME}' every {interval_minutes} min."


def uninstall() -> str:
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True, text=True)
    return "Removed." if r.returncode == 0 else (r.stderr.strip() or "Not installed.")
