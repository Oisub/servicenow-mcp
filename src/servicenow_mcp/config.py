"""Instance registry.

Instances live in ~/.servicenow-mcp/instances.json (no secrets).
Passwords live in the OS credential store (Windows Credential Manager) via keyring,
with an environment-variable fallback: SN_PASSWORD_<NAME> (name upper-cased, '-' -> '_').

`auth` is "basic" or "oauth". OAuth uses the password grant with an OAuth client registered on
the instance; its client_id is stored here, its secret in the credential store.
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
    auth: str = "basic"
    client_id: str = ""

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

    @property
    def client_secret(self) -> str:
        secret = keyring.get_password(KEYRING_SERVICE, _secret_key(self.name))
        if not secret:
            raise ConfigError(f"No OAuth client secret stored for '{self.name}'. Run add_instance again.")
        return secret


def _secret_key(name: str) -> str:
    return f"{name}:oauth_client_secret"


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
            auth=cfg.get("auth", "basic"),
            client_id=cfg.get("client_id", ""),
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
    old = raw.setdefault("instances", {}).get(name, {})
    entry = {"url": url, "username": username, "description": description}
    if old.get("auth") == "oauth" and old.get("url") == url:
        entry.update(auth="oauth", client_id=old.get("client_id", ""))  # keep the registered client
    raw["instances"][name] = entry
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
    for key in (name, _secret_key(name)):
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass


def set_oauth(name: str, client_id: str, client_secret: str) -> None:
    raw = _load_raw()
    if name not in raw.get("instances", {}):
        raise ConfigError(f"Unknown instance '{name}'")
    raw["instances"][name].update(auth="oauth", client_id=client_id)
    _save_raw(raw)
    keyring.set_password(KEYRING_SERVICE, _secret_key(name), client_secret)


def set_default(name: str) -> None:
    raw = _load_raw()
    if name not in raw.get("instances", {}):
        raise ConfigError(f"Unknown instance '{name}'")
    raw["default"] = name
    _save_raw(raw)
