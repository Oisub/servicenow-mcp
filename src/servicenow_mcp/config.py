"""Instance registry.

Instances live in ~/.servicenow-mcp/instances.json (no secrets).
Passwords live in the OS credential store (Windows Credential Manager) via keyring,
with an environment-variable fallback: SN_PASSWORD_<NAME> (name upper-cased, '-' -> '_').
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import keyring

KEYRING_SERVICE = "servicenow-mcp"
CONFIG_DIR = Path(os.environ.get("SERVICENOW_MCP_HOME", Path.home() / ".servicenow-mcp"))
CONFIG_FILE = CONFIG_DIR / "instances.json"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Instance:
    name: str
    url: str
    username: str
    description: str = ""

    @property
    def password(self) -> str:
        env_key = "SN_PASSWORD_" + self.name.upper().replace("-", "_")
        pw = os.environ.get(env_key) or keyring.get_password(KEYRING_SERVICE, self.name)
        if not pw:
            raise ConfigError(
                f"No password stored for instance '{self.name}'. "
                f"Run: servicenow-mcp add-instance {self.name} {self.url} {self.username}"
            )
        return pw


def _load_raw() -> dict:
    if not CONFIG_FILE.exists():
        return {"default": None, "instances": {}}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def _save_raw(raw: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def load_instances() -> tuple[dict[str, Instance], str | None]:
    raw = _load_raw()
    instances = {
        name: Instance(
            name=name,
            url=cfg["url"].rstrip("/"),
            username=cfg["username"],
            description=cfg.get("description", ""),
        )
        for name, cfg in raw.get("instances", {}).items()
    }
    return instances, raw.get("default")


def save_instance(name: str, url: str, username: str, password: str | None,
                  description: str = "", make_default: bool = False) -> None:
    url = url.rstrip("/")
    if not url.startswith("http"):
        url = f"https://{url}.service-now.com" if "." not in url else f"https://{url}"
    raw = _load_raw()
    raw.setdefault("instances", {})[name] = {
        "url": url, "username": username, "description": description,
    }
    if make_default or not raw.get("default"):
        raw["default"] = name
    _save_raw(raw)
    if password:
        keyring.set_password(KEYRING_SERVICE, name, password)


def remove_instance(name: str) -> None:
    raw = _load_raw()
    raw.get("instances", {}).pop(name, None)
    if raw.get("default") == name:
        raw["default"] = next(iter(raw["instances"]), None)
    _save_raw(raw)
    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass


def set_default(name: str) -> None:
    raw = _load_raw()
    if name not in raw.get("instances", {}):
        raise ConfigError(f"Unknown instance '{name}'")
    raw["default"] = name
    _save_raw(raw)
