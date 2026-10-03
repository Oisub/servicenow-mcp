"""Entry point.

  servicenow-mcp                                    run the MCP server (stdio)
  servicenow-mcp add-instance NAME URL USER [-d DESC] [--default]
                                                    add/update an instance; prompts for the password
  servicenow-mcp remove-instance NAME
  servicenow-mcp set-default NAME
  servicenow-mcp list
  servicenow-mcp test [NAME]                        check REST and background-script access
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from . import config


async def _test(name: str | None) -> int:
    from .client import SNClient

    instances, default = config.load_instances()
    names = [name] if name else list(instances)
    ok = True
    for n in names:
        c = SNClient(instances[n])
        try:
            rows = await c.table_get("sys_properties", sysparm_query="name=glide.buildtag.last",
                                     sysparm_fields="value", sysparm_limit=1)
            build = rows[0]["value"] if rows else "?"
            out = await c.run_background_script("gs.print('ok')")
            script_ok = "ok" in out
            print(f"{n}: REST ok (build {build}), background script {'ok' if script_ok else 'FAILED'}")
            ok &= script_ok
        except Exception as e:
            print(f"{n}: FAILED - {e}")
            ok = False
        finally:
            await c.close()
    return 0 if ok else 1


def main() -> None:
    if len(sys.argv) == 1:
        from .server import run
        run()
        return

    p = argparse.ArgumentParser(prog="servicenow-mcp")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-instance")
    a.add_argument("name")
    a.add_argument("url", help="https://devNNNN.service-now.com or just devNNNN")
    a.add_argument("username")
    a.add_argument("-d", "--description", default="")
    a.add_argument("--default", action="store_true")
    a.add_argument("--keep-password", action="store_true", help="don't prompt; keep stored password")
    sub.add_parser("remove-instance").add_argument("name")
    sub.add_parser("set-default").add_argument("name")
    sub.add_parser("list")
    sub.add_parser("test").add_argument("name", nargs="?")
    args = p.parse_args()

    if args.cmd == "add-instance":
        pw = None if args.keep_password else getpass.getpass(f"Password for {args.username}: ")
        config.save_instance(args.name, args.url, args.username, pw, args.description, args.default)
        print(f"Saved '{args.name}'. Password stored in the OS credential manager.")
    elif args.cmd == "remove-instance":
        config.remove_instance(args.name)
        print(f"Removed '{args.name}'.")
    elif args.cmd == "set-default":
        config.set_default(args.name)
        print(f"Default is now '{args.name}'.")
    elif args.cmd == "list":
        instances, default = config.load_instances()
        for i in instances.values():
            print(f"{'*' if i.name == default else ' '} {i.name:20} {i.url:45} {i.username}  {i.description}")
    elif args.cmd == "test":
        sys.exit(asyncio.run(_test(args.name)))


if __name__ == "__main__":
    main()
