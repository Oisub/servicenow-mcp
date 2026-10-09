# servicenow-mcp

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) do real
**ServiceNow development** work — not just read records, but run server-side scripts, debug ACLs,
flows and notifications, run ATF tests, analyse impact, and move update sets between instances.

**Who it's for:** ServiceNow developers who work across several personal developer / sub-prod
instances and want an AI assistant that can actually build, debug and verify on them.

**What makes it different from a plain Table-API wrapper:**

- **Runs server-side scripts** (Scripts - Background) in any application scope — full Glide API.
- **Debugs the hard things**: `check_access` impersonates a user and explains every ACL;
  `flow_executions`, `email_trace` and `run_atf` turn "why didn't this work" into one answer.
- **Impact analysis & audit**: `where_used` finds every reference to a table/field/script before you
  change it; `change_history` shows what changed, by whom, in which update set.
- **Multi-instance**: compare and copy records across instances, and migrate update sets through the
  platform's own export → preview → commit path.
- **Just works**: OAuth is set up automatically when an instance restricts Basic auth; PDIs can be
  kept awake; credentials live in the OS credential store, never in files or the chat.

### Example asks

Once connected, you can say things like:

- "Why can't `abel.tuter` edit INC0010023? Which ACL is blocking it?"
- "RITM0010005 didn't send its approval email — trace it."
- "What would break if I rename the `u_deal` field on the task table?"
- "What did the team change in `x_acme_app` this week?"
- "Run the 'Credit check routing' ATF test and show me which step failed."
- "Migrate the 'Approval rework' update set from dev to test, preview first."

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+, and an MCP client (e.g. Claude Code).

```powershell
git clone https://github.com/Oisub/servicenow-mcp
cd servicenow-mcp
uv sync
claude mcp add -s user servicenow -- "$PWD\.venv\Scripts\servicenow-mcp.exe"   # Windows
# macOS / Linux: claude mcp add -s user servicenow -- "$PWD/.venv/bin/servicenow-mcp"
```

The **first** ServiceNow request with no instance configured opens a small dialog to enter the
instance URL, username and password; the login is verified and the password is stored in your OS
credential manager before anything else runs. That's it — ask away.

> [!WARNING]
> **Security.** This server acts with the full rights of the account you give it — with an admin
> account that includes running arbitrary server-side scripts (`run_script`), deleting records and
> committing update sets. Anything the AI assistant decides to do, it can do on your instance.
> - Use it against **personal developer / sub-production instances**. Do not point it at production.
> - Prefer a dedicated account with only the roles you need; review write actions before approving them.
> - Passwords are kept in the OS credential store, never in files or in the conversation, but anyone
>   who can run code as your OS user can read them.
> - Provided as-is, without warranty. You are responsible for what it does on your instances.

## Instances

Instance list: `~/.servicenow-mcp/instances.json` (no secrets).
Passwords: OS credential store via keyring (Windows Credential Manager, macOS Keychain, ...;
service `servicenow-mcp`), or env `SN_PASSWORD_<NAME>`.

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

### Authentication

OAuth is the default; Basic auth is only a fallback. ServiceNow is phasing out Basic auth for APIs
(newer instances allow it only for users with the `snc_basic_auth_api_access` role). When an
instance is added (or with `uv run servicenow-mcp setup-auth [NAME]`), the server:

1. registers an OAuth client named `servicenow-mcp` on the instance through the UI session (no
   instance settings or roles are changed),
2. stores its client secret in the OS credential store and deletes the script-execution-history
   entry that contained it,
3. uses the OAuth password grant (ROPC) for REST from then on, renewing tokens automatically.

If OAuth cannot be set up (UI login unavailable, e.g. SSO-only, or ROPC disabled via
`glide.oauth.inbound.ropc.grant_type.disabled = true`), it falls back to Basic auth and says why.
`uv run servicenow-mcp list` shows which method each instance uses.

New instances are picked up without restarting Claude Code. In a conversation, switch with
`use_instance`, or pass `instance` to any tool.

### Keeping PDIs awake

Personal developer instances (`devNNNN.service-now.com`) hibernate after a period without
activity. The keep-alive logs in to each PDI through the UI and opens a page, like a developer would.

```powershell
uv run servicenow-mcp keepalive                    # once, now
uv run servicenow-mcp install-keepalive --every 60 # Windows Task Scheduler, no console window
uv run servicenow-mcp uninstall-keepalive
```

Results go to `~/.servicenow-mcp/keepalive.log`; the `keepalive_status` tool shows them. It only runs
while the PC is on, and it cannot wake an instance that is already hibernating (that needs the
developer portal) — it logs `HIBERNATING` instead. Check the Developer Program terms for your use.

## Tools

| Area | Tools |
|---|---|
| Instances | `list_instances`, `use_instance`, `add_instance` (dialog), `remove_instance`, `keepalive_status` |
| Schema | `describe_table` (fields incl. inherited, types, references) |
| Records | `query_records`, `get_record`, `create_record`, `update_record`, `delete_record`, `aggregate`, `upload_attachment` (attach a local file to a record) |
| Dev / debug | `run_script` (Scripts - Background, any scope), `search_scripts` (code search across script tables), `get_logs` (syslog) |
| Impact & audit | `where_used` (every code/condition and metadata reference to a table/field/script - impact analysis before a change), `change_history` (what changed from sys_update_xml, by time/app/user/type/update set) |
| Debugging | `check_access` (impersonate a user: read/write/create/delete + field checks, every relevant ACL with roles/condition/script result), `flow_executions` (Flow Designer runs: state, errors, logs, approvals for a record), `email_trace` (events → emails → notifications for a record, condition match, mail settings), `run_atf` (run a test or suite, per-step results) |
| Update sets | `list_update_sets`, `set_current_update_set` (also switches current application), `get_update_set_changes` |
| Update set migration | `migrate_update_set` (export → import → preview, optional commit), `resolve_preview_problems` (accept / skip), `commit_update_set` |
| Cross-instance | `compare_records` (diff dev vs test), `copy_records` (same sys_id, dry run first) |
| Browser hand-off | `ui_link` (exact URL of a form, list, new record, flow, flow execution, ATF runner...; results of the debugging tools also carry `ui_url`) |
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
- `check_access` impersonates the user inside a background script and always switches back. Deny-unless
  ACLs are shown as DENIES / does not deny; security attributes are listed but not evaluated.
- `flow_executions` shows step-level detail only if flow reporting is on
  (`com.snc.process_flow.reporting.level`); `run_atf` needs `sn_atf.runner.enabled=true`, and tests with
  UI steps need a client test runner open in a browser.
- Browser use (Claude in Chrome) is kept to what only the UI can show: the server's instructions tell
  the assistant to use the API tools first, open exact URLs from `ui_link`, check one thing and stop,
  and ask the user to log in instead of typing credentials.
- Hibernating PDIs are detected and reported; wake them at developer.servicenow.com.

## License

MIT © Dorian Shu. See [LICENSE](LICENSE).

> Not affiliated with or endorsed by ServiceNow, Inc. "ServiceNow" is a trademark of its owner.
