---
name: bash-mcp
description: "WSL bash executor MCP — REQUIRED way to run WSL commands from the agent. Triggers on 'wsl', 'bash', 'shell', 'command', 'exec', 'terminal', 'ubuntu', 'linux', or any prompt that requires running shell commands inside WSL. Use bash-mcp_run_command instead of `wsl -d ... -- bash -c \"...\"` from PowerShell — the latter has quoting, UTF-16, and PATH bugs. Other tools: bash_check_env (OS/PATH info), bash_list_binaries (known tools), bash_which (resolve binary), bash_mcp_status (service health), bash_mcp_classify (denylist explainer, v0.7), bash_mcp_audit_read (read audit backups, v0.7.1), bash_mcp_session_create/run/destroy/list (stateful cwd+env sessions, v0.6). Per-project cwd allowlists via `.bash-mcp.toml` (v0.8): drop a TOML in your repo root to extend or replace the global allowlist."
license: MIT
metadata:
  version: "1.9"
  category: tools
---

# bash-mcp — WSL Bash Executor (v0.8.0)

The **REQUIRED** way to run WSL bash commands from this agent. Replaces the old `wsl -d Ubuntu-22.04 -- bash -lc "..."` pattern.

## ⚠️ DO NOT use `wsl -d ... -- bash -c "..."`

```powershell
# ❌ WRONG — quoting, UTF-16, PATH bugs
wsl -d Ubuntu-22.04 -- bash -lc "cd /tmp && ls -lah"

# ✅ CORRECT — call the MCP tool instead
bash-mcp_run_command({command: "cd /tmp && ls -lah"})
```

A PreToolUse hook on the `bash` tool will **deny** any shell call that starts with `wsl` and ask you to retry with bash-mcp. If you see an abort reason mentioning "bash-mcp_run_command", that's the hook — switch tools and retry.

If you ever need to bypass the hook (e.g. testing), prefix the command with `BASH_MCP_SKIP=1`.

## Tools

### `bash-mcp_run_command(command, cwd?, timeout_ms?, env?, dangerous?)`

Execute bash, return structured output.

| Param | Type | Default | Notes |
|---|---|---|---|
| `command` | string | — | Required. Single bash command line. Runs via `bash -lc` (login shell, ~/.bashrc loaded). |
| `cwd` | string | `$HOME` | Absolute path. Must be under an allowed root (`$HOME`, `/tmp`, `/home`, `/mnt/c/Users/jesus`, `/var/tmp`). Windows-style paths (e.g. `C:\foo`) are auto-converted to `/mnt/c/foo`. |
| `timeout_ms` | int | `30000` | Hard cap `600000` (10 min). |
| `env` | object | `{}` | Additional env vars merged into inherited env (caller wins on conflict). |
| `dangerous` | bool | `false` | Bypass SOFT denylist. HARD denylist cannot be bypassed. |

Returns `{stdout, stderr, exit_code, duration_ms, timed_out, truncated, classification, audit_id}`.

Error shape (all errors follow this): `{error: {code, message, hint, documentation, matched_pattern?}}`. The `hint` field tells the agent what to do next.

### `bash-mcp_check_env()`

Read-only. Returns OS, kernel, Python version, uv version, `$HOME`, `$PATH` entries, `cwd`. Use this when you need to know what's available.

### `bash-mcp_list_binaries()`

Returns ~37 well-known tools with `{name, path, exists, version?}`. Sorted installed-first then alphabetical. Use this when you don't know if a tool is installed.

### `bash-mcp_which(name)`

Resolves a single binary. Returns `{name, path, exists, version?}`. Use for tools not in `list_binaries`.

### `bash_mcp_status()`

Read-only health check. Returns `{service, version, uptime_seconds, start_time, audit: {path, entries, size_bytes, max_bytes, backup_count, backups_present, gzip_threshold_bytes, gzip_enabled}, concurrency: {max_concurrent, active}, sessions: {active, max_env_per_session, idle_timeout_s, janitor_enabled}, python_version, process: {pid, rss_bytes}, tools: [...]}`. Use this to verify the service is up without SSH/tail logs.

### `bash-mcp_classify(command) → {...}` (v0.7)

Read-only denylist explainer. Does NOT execute. Use BEFORE `bash_run_command` if you're unsure whether a command will be rejected — saves a round-trip and gives you the exact regex + pattern that would fire.

Returns `{class, matched_pattern, pattern_index, hint_with_dangerous_false, hint_with_dangerous_true, would_execute, would_execute_with_dangerous_true, audit_id}` where `class ∈ {"safe", "dangerous", "reject"}`. Each call is audited as `tool="bash_mcp_classify"`.

### `bash-mcp_audit_read(backup_index, max_entries?, tool_filter?)` (v0.7.1)

Read entries from a rotated audit backup. Closes the v0.7 gzip one-way archival caveat by transparently decompressing `.jsonl.N.gz`. Returns `{backup_index, path, format ("plaintext" | "gzip"), entries, returned, audit_id}`. Reads are themselves audited as `tool="bash_mcp_audit_read"`. `backup_index=0` is the current `audit.jsonl`; 1..N are rotated backups.

## Stateful Sessions (v0.6)

A session is a server-side container that keeps a `cwd` and an accumulated `env` dict across calls. Use sessions when you need to:

- `cd` somewhere, run several commands there, then run more — your cwd persists.
- `export` a variable, run a tool that reads it, then run another tool that needs the same value — your env persists.
- Avoid re-sending the same `cwd` / `env` on every call.

**Important: sessions are process-lifetime.** A server restart wipes them. There is no auto-TTL; you must call `bash_mcp_session_destroy` when done.

### `bash-mcp_session_create(name?, cwd?) → {session_id, ...}`

Create a new session. Optional human-readable `name` (≤64 chars) and optional starting `cwd` (must be under an allowed root — same rules as `bash_run_command`).

### `bash-mcp_session_run(session_id, command, timeout_ms?, dangerous?) → result`

Run a command in a session. Same denylist, concurrency, timeout, and audit semantics as `bash-mcp_run_command`. Adds:

- Runs in `session.cwd` (instead of `$HOME` default).
- Inherits `session.env` (after `os.environ`).
- After `exit_code == 0`, parses top-level `cd PATH`, `export VAR=value`, `unset VAR` from the command text and updates the session.
- Returns `cwd` and `env_keys` in the response so you can confirm state.

**Parsing limitations** (documented; intentional for v0.6):
- Only top-level statements on simple command chains are tracked.
- Shell scripts, functions, `if`/`while` bodies, `$(...)`, backticks, heredocs, multi-line `\` continuations are out of scope.
- `export X="$HOME"` stores the literal `$HOME` (the subprocess still sees the real value because `bash -lc` expanded it; the session does not).

### `bash-mcp_session_destroy(session_id) → {ok, session_id, freed_env_vars}`

Destroy a session and free its memory. Returns `SESSION_NOT_FOUND` if the id is unknown.

### `bash-mcp_session_list() → [...]`

List active sessions. Most-recently-used first. Each entry: `{session_id, name, cwd, created_at, last_used_at, env_count, env_keys}`.

### Example

```javascript
// 1. Create a session in /tmp
const { session_id } = bash-mcp_session_create({name: "build", cwd: "/tmp"})

// 2. cd, set an env var, run a command — all in one call
bash-mcp_session_run({
  session_id, command: "cd /home/jbecerra/projects/myapp && export FOO=bar && pwd && echo $FOO"
})

// 3. Next call — cwd and env persist without re-sending
const r = bash-mcp_session_run({
  session_id, command: "pwd && echo $FOO"
})
// r.stdout → "/home/jbecerra/projects/myapp\nbar\n"

// 4. Cleanup
bash-mcp_session_destroy({session_id})
```

## Safety Model

### HARD denylist — always rejected (cannot be bypassed)

Block-device destruction:
- `rm -rf /` (except `/tmp`, `/home`, `/Users`)
- `dd of=/dev/{sd,hd,nvme,vd}*`
- `> /dev/{sd,hd,nvme,vd}*`
- `tee /dev/{sd,hd,nvme,vd}*`
- `cat|cp|mv → /dev/{sd,hd,nvme,vd}*` (block device write via file ops)
- `mkfs /dev/*`

Process / system destruction:
- `:(){ :|:& };:` (classic fork bomb) + variants (`:(){ :|& };`, `.(){ .|.& };.`, `bomb(){ bomb|bomb& };bomb`)
- `find ... -delete`
- `find ... -exec rm`
- `chmod -R NNN /` (except `/tmp`, `/home`, `/Users`)

Unrecoverable deletion:
- `shred /etc /var /usr /boot /bin /sbin/*`

Supply chain:
- `curl ... | bash`, `wget ... | bash`
- `sudo rm`

### SOFT denylist — requires `dangerous=true`

Privilege / process:
- `sudo` (any)
- `kill -9`, `kill -SIGKILL`
- `systemctl stop|disable|mask`

System changes:
- `git push --force`, `git push -f`
- `chown -R`
- `chmod NNN /`
- `> /etc/`

Package removal (v0.4):
- `apt remove|purge|autoremove`
- `pip uninstall`
- `npm uninstall|rm|remove -g|--global`

Package install (v0.0–v0.3):
- `pip install`
- `npm install -g`
- `apt(-get) install`

Destructive sync (v0.4):
- `rsync --delete`

If you intend one of these, **call `bash-mcp_run_command` with `dangerous: true`** explicitly.

### Operational notes

- All calls are audit-logged to `~/.local/share/bash-mcp/audit.jsonl` (JSONL).
- Audit log rotates at 25 MB (5 backups): `audit.jsonl.{1..5}`. Configurable via `BASH_MCP_AUDIT_MAX_BYTES` / `BASH_MCP_AUDIT_BACKUP_COUNT`.
- Max 8 concurrent subprocesses; excess calls queue. Configurable via `BASH_MCP_MAX_CONCURRENT`.
- Env values are NOT logged (only env keys).
- v0.6 sessions: per-session env dict is capped at 256 vars (configurable via `BASH_MCP_SESSION_MAX_ENV_VARS`). Sessions are process-lifetime — a restart wipes them. Caller must `bash_mcp_session_destroy` to free memory.
- v0.7 session auto-TTL (opt-in): set `BASH_MCP_SESSION_IDLE_TIMEOUT_S` to enable. A daemon janitor evicts sessions whose `last_used_at` is older than the timeout. Each eviction is audited as `tool="bash_mcp_session_destroy" reason="idle_ttl_expired"`. Sweep interval is `max(5, min(300, timeout // 4))` seconds. Default 0 = disabled (preserves v0.6 contract).
- v0.7 audit gzip-on-rotation (opt-in): set `BASH_MCP_AUDIT_GZIP_THRESHOLD_BYTES` to enable. After rotation, any backup larger than the threshold is gzipped to `.jsonl.N.gz` (one-way; use `zcat` to inspect). Default 0 = disabled (preserves v0.3 plaintext-only behavior).

## Common Patterns

```javascript
// Discover environment
bash-mcp_check_env()

// Check if a tool is installed
bash-mcp_list_binaries()  // batch
bash-mcp_which({name: "rg"})  // single

// Run a command
bash-mcp_run_command({command: "ls -lah /tmp"})

// Run with custom cwd and timeout
bash-mcp_run_command({command: "pytest -xvs", cwd: "/home/jbecerra/projects/myapp", timeout_ms: 120000})

// Run a dangerous command explicitly
bash-mcp_run_command({command: "sudo systemctl restart nginx", dangerous: true})

// Run a package removal explicitly (v0.4+)
bash-mcp_run_command({command: "pip uninstall requests", dangerous: true})

// Verify service health
bash-mcp_status()

// v0.6 — multi-step workflow with persistent cwd + env
const { session_id } = bash-mcp_session_create({name: "demo", cwd: "/tmp"})
bash-mcp_session_run({session_id, command: "cd /home/jbecerra/projects/myapp && export DEBUG=1"})
bash-mcp_session_run({session_id, command: "pwd && echo $DEBUG"})
bash-mcp_session_destroy({session_id})
```

## Operational Notes

- Server runs in WSL via systemd (`systemctl --user status bash-mcp.service`).
- Listens on `http://172.20.50.88:54321/mcp/` (WSL IP changes on reboot; run `./infra/update-ip.sh` from the repo root to refresh).
- Logs: `journalctl --user -u bash-mcp.service -f` or `screen -r bash-mcp`.
- Audit: `tail -f ~/.local/share/bash-mcp/audit.jsonl`.
- Deploy a fresh WSL/Windows setup: `./infra/install.sh` from the repo root.

## Per-project allowlists via `.bash-mcp.toml` (v0.8)

Projects can drop a `.bash-mcp.toml` at the repo root (or any ancestor) to override the global `ALLOWED_CWD_ROOTS` for calls whose cwd lives under that directory.

```toml
# mode is optional. Defaults to "extend".
mode = "extend"   # or "replace"

# required: list of paths the project\'s cwds may resolve under.
allowed_roots = [".", "frontend", "backend", "/srv/shared-cache"]
```

- **`extend`** (default) — merge the project's `allowed_roots` with the global list (de-duplicated).
- **`replace`** — replace the global list with the project's roots. Empty list = deliberate lockout (no cwd is allowed).

bash-mcp walks up from the call's cwd until it finds the first `.bash-mcp.toml` (or hits `$HOME` / filesystem root; capped at 32 hops). The result is cached for the process lifetime, keyed on `(start_dir, mtime_ns)` so editing the TOML invalidates the cache automatically.

**Failure modes are fail-soft** — malformed TOML, unknown mode, or non-list `allowed_roots` produce a stderr warning and bash-mcp falls back to the global allowlist. No crashes.

**Zero behavior change when no file is present.** The feature is opt-in per project; no install.sh change.
