# servicenow-mcp

ServiceNow MCP server for Claude Code, focused on development, debugging and keeping
multiple instances (dev / test / ...) in sync.

## Instances

Instance list: `~/.servicenow-mcp/instances.json` (no secrets).
Passwords: Windows Credential Manager (service `servicenow-mcp`), or env `SN_PASSWORD_<NAME>`.

```powershell
cd servicenow-mcp
uv run servicenow-mcp add-instance test dev123456 admin -d "test env"   # prompts for password
uv run servicenow-mcp list
uv run servicenow-mcp set-default dev
uv run servicenow-mcp test            # REST + background script check for every instance
uv run servicenow-mcp remove-instance test
```

Or just ask Claude to add an instance: the `add_instance` tool opens a desktop dialog
(URL / username / password, login is verified before saving). The password never passes
through the conversation. `remove_instance` removes one.

New instances are picked up without restarting Claude Code. In a conversation, switch with
`use_instance`, or pass `instance` to any tool.

## Tools

| Area | Tools |
|---|---|
| Instances | `list_instances`, `use_instance`, `add_instance` (dialog), `remove_instance` |
| Schema | `describe_table` (fields incl. inherited, types, references) |
| Records | `query_records`, `get_record`, `create_record`, `update_record`, `delete_record`, `aggregate` |
| Dev / debug | `run_script` (Scripts - Background, any scope), `search_scripts` (code search across script tables), `get_logs` (syslog) |
| Update sets | `list_update_sets`, `set_current_update_set` (also switches current application), `get_update_set_changes` |
| Update set migration | `migrate_update_set` (export → import → preview, optional commit), `resolve_preview_problems` (accept / skip), `commit_update_set` |
| Cross-instance | `compare_records` (diff dev vs test), `copy_records` (same sys_id, dry run first) |
| Escape hatch | `rest_request` (any REST endpoint) |

## Notes

- `run_script` logs in through the UI (`login.do`) and posts to `sys.scripts.do`; needs admin.
  In scoped apps use `gs.info()` instead of `gs.print()`.
- `set_current_update_set` uses `GlideUpdateSet.set()` on the instance; writing
  `sys_user_preference` directly does not work because preferences are cached.
- `migrate_update_set` uses the same path as the UI: `UpdateSetExport` + `export_update_set.do` on
  the source, `sys_upload.do` (Import Update Set from XML) on the target, then the UI's preview and
  commit workers. Commit is refused while preview problems are unresolved. Batch update sets are
  not supported yet.
- REST calls are stateless (no session cookie) so a switched update set / application is honoured
  immediately; `set_current_update_set` also persists `apps.current_app`.
- Hibernating PDIs are detected and reported; wake them at developer.servicenow.com.
