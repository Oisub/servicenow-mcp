"""Dialog for adding an instance. The password goes straight to the OS credential store and is
never printed. Prints a JSON result (without the password) to stdout."""

from __future__ import annotations

import json
import sys
import tkinter as tk
from tkinter import messagebox, ttk

import httpx

from . import config


def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = f"https://{url}.service-now.com" if "." not in url else f"https://{url}"
    return url


def _check_login(url: str, username: str, password: str) -> str | None:
    """Return None if the credentials work, else a readable error."""
    try:
        r = httpx.get(f"{url}/api/now/table/sys_user", auth=(username, password), timeout=30,
                      params={"sysparm_limit": 1, "sysparm_fields": "sys_id"},
                      headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        return f"无法连接 {url}\n{e}"
    if r.status_code == 401:
        return "用户名或密码错误（HTTP 401）。"
    if "text/html" in r.headers.get("content-type", ""):
        return "实例返回了网页而不是数据，可能正在休眠或 URL 不对。"
    if r.status_code >= 400:
        return f"HTTP {r.status_code}: {r.text[:200]}"
    return None


def run(prefill_name: str = "") -> dict:
    result: dict = {"saved": False}
    root = tk.Tk()
    root.title("添加 ServiceNow 实例")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    frame = ttk.Frame(root, padding=16)
    frame.grid()
    fields = {}
    rows = [
        ("name", "名称（用于切换，如 test）", False),
        ("url", "实例名或 URL（如 dev123456）", False),
        ("username", "用户名", False),
        ("password", "密码", True),
        ("description", "说明（可选）", False),
    ]
    for i, (key, label, secret) in enumerate(rows):
        ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 12))
        entry = ttk.Entry(frame, width=36, show="•" if secret else "")
        entry.grid(row=i, column=1, pady=4)
        fields[key] = entry
    if prefill_name:
        fields["name"].insert(0, prefill_name)
    fields["username"].insert(0, "admin")

    make_default = tk.BooleanVar(value=False)
    ttk.Checkbutton(frame, text="设为默认实例", variable=make_default).grid(
        row=len(rows), column=1, sticky="w", pady=(4, 8))
    status = ttk.Label(frame, text="", foreground="#b00020")
    status.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w")

    def save() -> None:
        v = {k: e.get().strip() for k, e in fields.items()}
        v["password"] = fields["password"].get()  # keep spaces in passwords
        missing = [label for key, label, _ in rows[:4] if not v[key]]
        if missing:
            status.config(text="请填写：" + "、".join(missing))
            return
        url = _normalize_url(v["url"])
        status.config(text="正在验证登录…", foreground="#555")
        root.update()
        err = _check_login(url, v["username"], v["password"])
        if err and not messagebox.askyesno("验证失败", f"{err}\n\n仍然保存吗？", parent=root):
            status.config(text=err.splitlines()[0], foreground="#b00020")
            return
        config.save_instance(v["name"], url, v["username"], v["password"],
                             v["description"], make_default.get())
        result.update(saved=True, name=v["name"], url=url, username=v["username"],
                      default=make_default.get(), login_verified=err is None)
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="e", pady=(8, 0))
    ttk.Button(buttons, text="取消", command=root.destroy).grid(row=0, column=0, padx=4)
    ttk.Button(buttons, text="验证并保存", command=save).grid(row=0, column=1)
    root.bind("<Return>", lambda _e: save())
    root.bind("<Escape>", lambda _e: root.destroy())

    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f"+{x}+{y}")
    root.lift()
    root.focus_force()
    (fields["url"] if prefill_name else fields["name"]).focus_set()
    root.mainloop()
    return result


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1] if len(sys.argv) > 1 else ""), ensure_ascii=False))
