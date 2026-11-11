# bash-mcp

A small MCP server that exposes WSL bash as a structured tool to MiniMax Code. Replaces the old `wsl -d ... -- bash -c "..."` PowerShell pattern with a single tool call.

## Why

Running WSL commands from PowerShell (`wsl -d Ubuntu-22.04 -- bash -lc "..."`) has daily friction costs:

- **Quoting hell** — PowerShell reinterprets `$()` as `Get-Date`, backticks, and regex escapes break.
- **UTF-16 garbage** — `wsl.exe` to PowerShell pipe occasionally emits garbled output.
- **Stateless shells** — each call is a fresh shell; no cwd, env, or history.
- **Output is unstructured** — stdout/stderr mixed, no exit code, no duration.
- **No safety net** — a single bad `rm -rf` quote wipes the home.

`bash-mcp` solves all of these with one tool call returning `{stdout, stderr, exit_code, duration_ms, timed_out, truncated, classification, audit_id}`.

## License

[MIT](LICENSE)

## What's new in v0.10.0

- **The gzip-on-rotation pass no longer destroys backups.** The v0.7 gzip pass
  compressed a rotated backup, but the rotation shift only renamed *plaintext*
  backups (`audit.jsonl.N`). Gzipped backups (`audit.jsonl.N.gz`) were never
  shifted, so they kept their index — and the next gzip pass wrote a fresh
  `audit.jsonl.1.gz` straight over the existing one. **With gzip enabled,
  every rotation silently destroyed the oldest backup.** The shift now moves
  both variants, and the gzip pass never overwrites an existing archive (it
  warns and keeps both files instead).
- **The gzip pass is on by default, above a 1 MiB threshold.** Until now
  `BASH_MCP_AUDIT_GZIP_THRESHOLD_BYTES` defaulted to `0` (= disabled), so the
  feature existed but never ran unless you opted in. The default is now
  `1048576` (1 MiB): a rotated backup larger than that is compressed to
  `audit.jsonl.N.gz` and its plaintext is removed. Set the env var to `0` to
  opt out and keep plaintext-only rotation.
- **`bash_mcp_status` gained `audit.{gzip_backups, plaintext_backups}`** — how
  many rotated backups are compressed vs. plaintext, so the rotation can be
  verified at a glance.
- 347 tests passing (up from 338).

## What's new in v0.9.0

- **Sessions survive a server restart** — every `bash_mcp_session_*` mutation is
  mirrored to a SQLite store (`~/.local/share/bash-mcp/sessions.db`, stdlib
  `sqlite3`, no new dependency). On boot the server restores the sessions that
  were alive before, so a WSL reboot no longer wipes the agent's cwd/env state.
  - **New module `src/bash_mcp/session_store.py`** — one row per session
    (`session_id`, `name`, `cwd`, `env`, `created_at`, `last_used_at`). One
    connection per operation (FastMCP runs tools in a thread pool), so the store
    is thread-safe without extra locking.
  - **Fail-soft, always** — a missing directory, an unwritable path or a
    corrupt database degrades to "no persistence"; it never raises into a tool
    call. Sessions keep working in memory.
  - **Opt out** — `BASH_MCP_SESSION_PERSIST=0` restores the pre-v0.9
    process-lifetime behaviour (nothing is written, nothing is read).
  - **Relocate** — `BASH_MCP_SESSION_DB_PATH` overrides the location.
  - **Restore rules** — a session whose `cwd` was deleted, that is no longer
    under an allowed root, or whose id is malformed is dropped from the store
    instead of being restored. The env dict is truncated to `MAX_ENV_VARS`.
  - **Idle eviction deletes the row too**, so an evicted session does not come
    back on the next boot.
  - **`bash_mcp_status`** gained `sessions.persistence.{enabled, path,
    persisted_sessions}`.
- **12 tools, no new tool** — v0.9 changes the lifecycle of the existing
  session tools; it does not add any.

## What's new in v0.3.0

- **Audit log rotation** — `audit.jsonl` rotates at 25 MB (configurable) keeping 5 backups (`audit.jsonl.{1..5}`). Lazy + thread-safe.
- **Concurrency limit** — max 8 concurrent subprocesses via `threading.BoundedSemaphore` at the executor boundary. Excess calls queue, never drop.
- **`bash_mcp_status` extended** with `audit.{max_bytes, backup_count, backups_present}` and `concurrency.{max_concurrent, active}` fields.
- 95 tests passing (up from 84).

## What's new in v0.8.0

- **Per-project cwd allowlists via `.bash-mcp.toml`** — drop a TOML file at
  your repo root (or any ancestor) and bash-mcp picks it up for calls whose
  cwd lives under that directory. Two modes: ``extend`` (default; merge with
  global) and ``replace`` (sandbox; replace global). Zero behavior change when
  no file is present. See **Configuration → `.bash-mcp.toml`** below for the
  schema. No new MCP tool — v0.8 is purely a config-driven feature.

## What's new in v0.7.1

- **`bash_mcp_audit_read(backup_index, max_entries?, tool_filter?)`** — closes the v0.7 gzip one-way archival caveat. Transparently decompresses `audit.jsonl.N.gz` backups in addition to plaintext. Returns a stable `{backup_index, path, format, entries, returned}` object. Audited as `tool="bash_mcp_audit_read"`.
- **Cleanup** — deleted stray `infra/install.sh.before-addendum` (untracked, never committed).
- 12 tools total (up from 11). 289 tests passing (up from 274). 86 smoke cases (up from 78).

All changes are non-breaking. v0.7.0 callers see zero behavior change.

## What's new in v0.7.0

- **`bash_mcp_classify(command)` denylist explainer** — read-only tool that returns `{class, matched_pattern, pattern_index, hint_with_dangerous_false, hint_with_dangerous_true, would_execute, would_execute_with_dangerous_true, audit_id}`. Use BEFORE `bash_run_command` if you're unsure whether a command will be rejected. Saves a round-trip and gives the exact regex that would fire.
- **Session auto-TTL (opt-in)** — set `BASH_MCP_SESSION_IDLE_TIMEOUT_S` to enable. A daemon janitor evicts sessions whose `last_used_at` is older than the timeout. Each eviction is audited as `tool="bash_mcp_session_destroy" reason="idle_ttl_expired"`. Default 0 = disabled (preserves v0.6 contract).
- **Audit log gzip-on-rotation (opt-in)** — set `BASH_MCP_AUDIT_GZIP_THRESHOLD_BYTES` to enable. After rotation, any backup larger than the threshold is gzipped to `.jsonl.N.gz` (one-way; use `zcat` to inspect). Default 0 = disabled (preserves v0.3 plaintext-only behavior). _(Since v0.10 the default is 1 MiB and the pass no longer destroys the oldest backup — see "What's new in v0.10.0".)_
- **`bash_mcp_status` extended** with `audit.{gzip_threshold_bytes, gzip_enabled}` and `sessions.{idle_timeout_s, janitor_enabled}`.
- 274 tests passing (up from 220).

All three features are **opt-in via env vars** and default to off, so existing callers see zero behavior change.

## What's new in v0.6.0

- **Stateful sessions** — four new tools (`bash_mcp_session_create`, `_run`, `_destroy`, `_list`) keep a server-side `cwd` and accumulated `env` dict across calls. Use them when you need to `cd` somewhere and stay there, or `export` a variable and have it persist for the next call.
- **Sessions are process-lifetime** — a server restart wipes them. Caller is responsible for `_destroy`. There is no auto-TTL. _(Superseded in v0.9: sessions are persisted to disk and restored on boot.)_
- **Best-effort parser** — top-level `cd PATH`, `export VAR=value`, `unset VAR` are tracked; shell scripts, functions, `$(...)`, heredocs are out of scope (the subprocess still runs correctly, but state isn't persisted from them).
- **`bash_mcp_status` extended** with `sessions.{active, max_env_per_session}`.
- 220 tests passing (up from 164).

See [CHANGELOG.md](CHANGELOG.md) for full history.

## What's new in v0.4.0

- **Quarterly denylist review #1** — 6 new HARD patterns + 4 new SOFT patterns (see Safety table below).
- **Denylist review history** in `safety.py` documenting all reviews to date (v0.1, v0.2, v0.3, v0.4).
- 164 tests passing (up from 95).

## Tools

| Tool | Purpose |
|---|---|
| `bash_mcp_run_command(command, cwd?, timeout_ms?, env?, dangerous?)` | Execute bash. Returns structured output. |
| `bash_mcp_check_env()` | OS, kernel, Python, uv, PATH entries. |
| `bash_mcp_list_binaries()` | ~37 well-known tools with `{path, exists, version?}`. |
| `bash_mcp_which(name)` | Resolve a single binary. |
| `bash_mcp_status()` | Service health, audit stats + rotation config, concurrency, sessions, uptime, tool list. |
| `bash_mcp_session_create(name?, cwd?)` | Create a stateful session; returns a `session_id`. Persisted to disk since v0.9. |
| `bash_mcp_session_run(session_id, command, ...)` | Run a command in a session; persists `cd` / `export` / `unset`. |
| `bash_mcp_session_destroy(session_id)` | Destroy a session, free memory, drop the disk row. |
| `bash_mcp_session_list()` | List active sessions, most-recently-used first (includes sessions restored on boot). |
| `bash_mcp_classify(command)` | Read-only denylist explainer; returns `{class, matched_pattern, hint, would_execute, ...}`. v0.7. |
| `bash_mcp_audit_read(backup_index, max_entries?, tool_filter?)` | Read entries from a rotated audit backup (plaintext or gzip). v0.7.1. |

The server exposes its tools with **underscore** names (`bash_run_command`, `bash_mcp_status`, `bash_mcp_session_create`, ...). MiniMax Code prefixes them with the server name, so the tool you actually call is `mcp__bash-mcp__bash_run_command`. The hyphenated `bash-mcp_run_command` **does not exist** on the server.

## Quick start (for the agent)

```javascript
// Discover environment
bash_mcp_check_env()

// Check if a tool is installed
bash_mcp_list_binaries()       // batch
bash_mcp_which({name: "rg"})   // single

// Run a command
bash_mcp_run_command({command: "ls -lah /tmp"})

// Run with custom cwd and timeout
bash_mcp_run_command({command: "pytest -xvs", cwd: "/home/jbecerra/projects/myapp", timeout_ms: 120000})

// Bypass the SOFT denylist
bash_mcp_run_command({command: "sudo systemctl restart nginx", dangerous: true})
bash_mcp_run_command({command: "pip uninstall requests", dangerous: true})

// Verify service health (audit + concurrency state)
bash_mcp_status()
```

## ⚠️ DO NOT use `wsl -d ... -- bash -c "..."`

A `PreToolUse` hook in MiniMax Code **denies** any shell call whose command starts with `wsl(\.exe)?\s`. If you trigger it, you'll see an abort reason pointing you to this README.

**Escape hatch** (for genuine testing): prefix the command with `BASH_MCP_SKIP=1`.

## Architecture

```
MiniMax Code ──HTTP POST──> bash-mcp server (WSL, port 54321)
                            │
                            ├─ classify(command) → safe / dangerous / reject
                            ├─ acquire concurrency.slot()  (BoundedSemaphore, max 8)
                            ├─ subprocess.run([bash, "-lc", command], ...)
                            ├─ truncate stdout/stderr to 50KB
                            ├─ audit.log(...) → audit.jsonl
                            └─ on size > 25 MB: rotate → audit.jsonl.{1..5}
```

Streamable-http transport. `bash_mcp_run_command` is stateless per call; the
four `bash_mcp_session_*` tools additionally clone + write `cwd` and `env`
through `src/bash_mcp/sessions.py` (process-lifetime only). v0.7 adds a
daemon janitor thread (opt-in via `BASH_MCP_SESSION_IDLE_TIMEOUT_S`) and
gzip-on-rotation for the audit log (on by default above 1 MiB via
`BASH_MCP_AUDIT_GZIP_THRESHOLD_BYTES`; `0` disables it).

## Safety

Full list lives in `src/bash_mcp/safety.py`. Snapshot:

**HARD denylist** (always rejected, cannot be bypassed):

| Category | Patterns |
|---|---|
| Block-device destruction | `rm -rf /` (except `/tmp`, `/home`, `/Users`); `dd of=/dev/{sd,hd,nvme,vd}*`; `> /dev/{sd,hd,nvme,vd}*`; `tee /dev/{sd,hd,nvme,vd}*`; `cat\|cp\|mv → /dev/sdX`; `mkfs /dev/*` |
| Process / system | fork bombs (`:(){ :\|:& };:` + 3 variants); `find ... -delete`; `find ... -exec rm`; `chmod -R NNN /` |
| Unrecoverable delete | `shred /etc /var /usr /boot /bin /sbin/*` |
| Supply chain | `curl ... \| bash`; `wget ... \| bash`; `sudo rm` |

**SOFT denylist** (requires `dangerous=true`):

| Category | Patterns |
|---|---|
| Privilege / process | `sudo`; `kill -9`; `kill -SIGKILL`; `systemctl stop\|disable\|mask` |
| System changes | `git push --force` / `-f`; `chown -R`; `chmod NNN /`; `> /etc/` |
| Package removal (v0.4) | `apt remove\|purge\|autoremove`; `pip uninstall`; `npm uninstall\|rm\|remove -g\|--global` |
| Package install | `pip install`; `npm install -g`; `apt(-get) install` |
| Destructive sync (v0.4) | `rsync --delete` |

Every error response includes `hint` (actionable next step) and `documentation` (path to the skill) — the agent does not need to parse free text to recover.

`cwd` is allowlist-validated: must resolve under `$HOME`, `/tmp`, `/home`, `/mnt/c/Users/jesus`, or `/var/tmp`. Windows-style paths (`C:\foo`) are auto-converted to `/mnt/c/foo`.

Denylist is reviewed quarterly. Last review: **v0.4 (2026-09-12)** — see `safety.py` for full history.

## Configuration

All via environment variables. Set them in the `Environment=` lines of `infra/systemd/bash-mcp.service` (the reference unit), export them in your shell before running locally, or add them to the `screen` launch command in `infra/launcher.sh`.

> **This WSL instance runs with `systemd=false`** in `/etc/wsl.conf`, so `systemctl --user` is unavailable (`Failed to connect to bus`) and the server runs in a `screen` session instead. See **Operations -> Start / stop / restart**.

| Variable | Default | Purpose |
|---|---|---|
| `FASTMCP_SERVER_HOST` | `0.0.0.0` | Bind address |
| `FASTMCP_SERVER_PORT` | `54321` | Bind port (IANA dynamic range) |
| `BASH_MCP_AUDIT_MAX_BYTES` | `26214400` (25 MB) | Audit log rotation threshold |
| `BASH_MCP_AUDIT_BACKUP_COUNT` | `5` | Audit log backups kept |
| `BASH_MCP_AUDIT_GZIP_THRESHOLD_BYTES` | `1048576` (1 MiB) | Rotated backups above this size are gzipped. `0` disables (v0.10) |
| `BASH_MCP_MAX_CONCURRENT` | `8` | Max in-flight subprocesses |
| `BASH_MCP_SESSION_PERSIST` | `1` | Persist sessions to disk (v0.9). `0` = off |
| `BASH_MCP_SESSION_DB_PATH` | `$XDG_DATA_HOME/bash-mcp/sessions.db` | Session store location (v0.9) |

### `.bash-mcp.toml` (per-project cwd allowlist, v0.8)

Drop a `.bash-mcp.toml` at any directory in your project. bash-mcp walks up
from the call's cwd to find it (stops at `$HOME` or filesystem root, max 32
hops) and applies the resulting allowlist to that call. No new tool, no
schema-validation library, no install hook.

**Schema:**

```toml
# mode is optional. Defaults to "extend".
mode = "extend"   # or "replace"

# required: list of paths the project's cwds may resolve under.
# Relative entries resolve against the TOML's parent dir;
# absolute entries (after ~ expansion) are used as-is.
# Existence is NOT checked here — executor.run checks separately.
allowed_roots = [".", "frontend", "backend", "/srv/shared-cache"]
```

**Modes:**

- ``extend`` (default) — *add* the project's ``allowed_roots`` to the global
  ``ALLOWED_CWD_ROOTS``. Use this when your project needs to expose
  additional worktrees, build dirs, or shared caches outside the global
  allowlist (e.g. ``/srv/shared-cache`` above).
- ``replace`` — *replace* the global list with the project's roots. Use
  this for sandboxed environments where the global list is too permissive.
  An empty ``allowed_roots`` list is a deliberate lockout (no cwd is
  allowed; the caller receives ``INVALID_CWD``).

**Failure modes (all fail-soft — bash-mcp falls back to the global
allowlist, no crash):**

- Malformed TOML → stderr warning, global roots apply.
- Unknown ``mode`` value → stderr warning, defaults to ``extend``.
- ``allowed_roots`` is not a list of strings → stderr warning, global
  roots apply.

**Cache:** the loaded allowlist is cached per ``(start_dir, mtime_ns)`` for
the lifetime of the bash-mcp process. Editing the TOML invalidates the
cache; deleting the TOML means the next call from that directory re-walks
the tree.

## Deploying

`infra/install.sh` deploys everything to a fresh WSL/Windows setup:

- systemd unit → `~/.config/systemd/user/bash-mcp.service` (reference unit — on a `systemd=false` WSL the server actually runs in a `screen` session)
- PreToolUse hook → `~/.minimax/agents/mavis/hooks/bash-mcp-redirect.md`
- Skill → `~/.minimax/skills/bash-mcp/` (and `~/.mavis/skills/bash-mcp/` for the legacy runtime)
- mcp.json entry → `~/.minimax/mcp/mcp.json`
- Agent system_prompt addendum → reported on stdout by `install.sh [5/5]`; applied manually once per environment via the desktop `mavis agent update mavis` tool (the WSL and Windows `mavis` CLIs do not expose the `agent` subcommand)

The script also prints the one `mavis mcp create` command that must be
run from Windows PowerShell (the runtime registry is Windows-side).

```bash
./infra/install.sh
```

### Agent integration

bash-mcp ships an `infra/system-prompt-addendum.md` that, when applied, is
appended to the `mavis` agent's `systemPrompt`. This is the agent-side guardrail
that prevents the model from falling back to the `wsl -d ... -- bash -c "..."`
pattern from PowerShell — even when the PreToolUse hook times out or the skill
isn't auto-loaded.

**Auto-apply is not supported.** The `mavis` CLI on WSL is the IDE launcher
(`start` / `stop` / `status` only), and the Windows `mavis.cmd` references
`daemon/cli.js` which is not bundled in current installs. Neither CLI exposes
`agent update`. The apply must be performed by the desktop `mavis` MCP tool,
which is what `install.sh [5/5]` documents on stdout.

**Manual apply** (run once per environment, anywhere with the desktop tool):

```text
# install.sh prints the exact 3-step command (snapshot → build → apply)
# on stdout. The canonical addendum is at infra/system-prompt-addendum.md.
# Idempotent: rerun is a no-op if the marker substring is already in system_prompt.
```

**Why this lives in bash-mcp, not in the agent repo.** bash-mcp is the project
that owns the `bash-mcp`-vs-`wsl-d` rule across all three sync points (skill,
hook, mcp.json description). The system_prompt addendum is the fourth sync
point — same wording, same escape hatch (`BASH_MCP_SKIP=1`), same cross-references
to this README. Keeping all four together is what AGENTS.md means by "four places
that must stay in sync".

## Operations

### Start / stop / restart

```bash
# Start (idempotent — waits for port 54321, retries up to 3 times)
bash ~/projects/bash-mcp/infra/launcher.sh

# Attach to the server's screen session (that is where the logs are)
screen -r bash-mcp        # detach again with Ctrl-A d

# Stop
screen -S bash-mcp -X quit

# Health / config snapshot
bash_mcp_status()
```

> **Why not `systemctl`?** This WSL instance runs with `systemd=false` in
> `/etc/wsl.conf`, so `systemctl --user start bash-mcp.service` is a
> **silent no-op** (`Failed to connect to bus`) — the old launcher called it
> and never reported an error, so bash-mcp simply did not come back after a
> reboot. The unit file in `infra/systemd/` is kept as the reference for the
> `Environment=` lines and the `screen` `ExecStart`.

### Architecture diagram

`architecture.mermaid` is the source of truth; `architecture.html` is its
standalone render (open it in any browser). Regenerate after editing the
mermaid source:

```bash
uv run python infra/scripts/build-architecture.py        # write architecture.html
uv run python infra/scripts/build-architecture.py --check  # CI: fail if stale
```

### Prove a session survives a restart (v0.9)

```bash
# 1. create a session with a distinctive cwd + env
uv run python infra/scripts/proof-persistence.py create

# 2. restart the server, then check the session came back
screen -S bash-mcp -X quit && bash ~/projects/bash-mcp/infra/launcher.sh
uv run python infra/scripts/proof-persistence.py check <session_id>
```

### Audit log (rotation in v0.3+)

The audit log at `~/.local/share/bash-mcp/audit.jsonl` rotates automatically when it crosses `BASH_MCP_AUDIT_MAX_BYTES` (default 25 MB).

```bash
# Tail the current log
tail -f ~/.local/share/bash-mcp/audit.jsonl

# Pretty-print last 5 entries
tail -5 ~/.local/share/bash-mcp/audit.jsonl | jq .

# Check current size + rotation config
bash_mcp_status  # → audit.size_bytes, audit.max_bytes, audit.backup_count, audit.backups_present

# List existing backups (audit.jsonl.{1..5})
ls ~/.local/share/bash-mcp/

# Read a specific backup
less ~/.local/share/bash-mcp/audit.jsonl.1 | jq .
```

Rotation behavior:
- Lazy — happens on next `log()` call after the threshold is crossed.
- Thread-safe — `audit.jsonl` and the rotation move happen under one lock.
- Fail-soft — `OSError` on rename prints to stderr; next call retries.
- Naming — `audit.jsonl.{N}` (stdlib convention); oldest is dropped when count exceeds `BASH_MCP_AUDIT_BACKUP_COUNT`.
- No content loss — verified by `test_rotation_preserves_content`.

### Concurrency (v0.3+)

Max 8 concurrent subprocesses by default. Excess calls queue (don't drop). Tune via `BASH_MCP_MAX_CONCURRENT`.

```bash
# Check current limit + active count
bash_mcp_status  # → concurrency.max_concurrent, concurrency.active
```

If you make 20 parallel tool calls, 8 run, 12 queue. The semaphore is at the executor boundary, so `bash_run_command`, `bash_check_env`'s version probes, and any future subprocess-using tool all share the same cap.

### Health check (no SSH/curl needed)

```bash
# Via MiniMax Code (recommended):
bash_mcp_status

# Or via shell + curl:
curl http://localhost:54321/mcp/  # then initialize + tools/call bash_mcp_status
```

### WSL IP changed (after reboot)

```bash
cd /home/jbecerra/projects/bash-mcp
./infra/update-ip.sh
```

This rewrites only the `bash-mcp` URL in `~/.minimax/mcp/mcp.json` (and `~/.cursor/mcp.json` if present). Does not touch `semantic-memory`.

### Bootstrap on Windows login

`infra/launcher.sh` ships out of the box. To run it automatically on every Windows logon, register the scheduled task (run as Administrator from PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File C:\path\to\bash-mcp\infra\windows\install-task.ps1
```

This registers `BashMcp-WSL-Bootstrap` in Task Scheduler with a 60s delay, RunLevel=Highest, action = `wsl.exe /home/jbecerra/projects/bash-mcp/infra/launcher.sh`. The launcher starts bash-mcp in a `screen` session and waits for port 54321 (idempotent: if the port is already open it exits immediately).

> **Not registered on this machine.** The XML ships in the repo but the task was never installed here — `Get-ScheduledTask -TaskName BashMcp-WSL-Bootstrap` returns nothing. Until it is registered, bash-mcp must be started manually after every reboot: `bash ~/projects/bash-mcp/infra/launcher.sh`.

Verify:

```powershell
Get-ScheduledTask -TaskName BashMcp-WSL-Bootstrap | Format-List
```

Remove:

```powershell
Unregister-ScheduledTask -TaskName BashMcp-WSL-Bootstrap -Confirm:$false
```

The `install.sh` script (when run without `--dry-run`) prints the same install-task.ps1 path as a hint.

## Development

### Install

```bash
source ~/.local/bin/env
export PATH="$HOME/.local/bin:$PATH"
cd /home/jbecerra/projects/bash-mcp
uv sync
```

### Run locally

```bash
uv run python -m bash_mcp.server --transport streamable-http --host 0.0.0.0 --port 54321
```

### Run tests

```bash
uv run pytest tests/ -v
```

**164 tests** cover:

- 115 safety cases (test_safety.py) — parametrized denylist + safe baselines + review-history
- 27 executor cases (test_executor.py) — subprocess + cwd allowlist + Windows→WSL
- 9 error envelope cases (test_errors.py)
- 6 audit log rotation cases (test_audit_rotation.py)
- 5 concurrency limit cases (test_concurrency.py)
- 11 e2e tests against the live server (test_e2e.py)

### Project layout

```
bash-mcp/
├── AGENTS.md                        # project-scoped rules for future agents
├── CHANGELOG.md                     # version history
├── LICENSE                          # MIT
├── README.md                        # this file
├── architecture.mermaid             # architecture diagram — source of truth
├── architecture.html                # architecture diagram — standalone render (generated)
├── pyproject.toml                   # uv-managed, FastMCP 2.7.0
├── uv.lock
├── src/bash_mcp/                    # server code
│   ├── server.py                    # FastMCP + 12 tools
│   ├── executor.py                  # subprocess wrapper, timeout, truncation, cwd allowlist, concurrency slot
│   ├── safety.py                    # classify(command) + quarterly denylist reviews
│   ├── audit.py                     # JSONL append-only logger + size-based rotation
│   ├── discovery.py                 # which / list_binaries (thread-pool)
│   ├── concurrency.py               # BoundedSemaphore wrapper
│   ├── sessions.py                  # stateful sessions (cwd + env across calls)
│   └── project_allowlist.py         # per-project .bash-mcp.toml resolution
├── tests/                           # 281 tests (24 e2e skipped without a live server)
│   ├── test_safety.py               # denylist cases
│   ├── test_executor.py             # subprocess + cwd allowlist
│   ├── test_errors.py               # error envelope shape + hints
│   ├── test_audit_rotation.py       # rotation cases
│   ├── test_audit_gzip.py           # gzip-on-rotation cases
│   ├── test_audit_gzip_rotation.py  # gzip rotation lifecycle (v0.10)
│   ├── test_audit_read.py           # bash_mcp_audit_read cases
│   ├── test_classify_tool.py        # bash_mcp_classify cases
│   ├── test_concurrency.py          # concurrency limit cases
│   ├── test_sessions.py             # session create / run / destroy / list
│   ├── test_sessions_ttl.py         # idle TTL janitor
│   ├── test_project_allowlist.py    # .bash-mcp.toml resolution
│   └── test_e2e.py                  # live-server round-trips
└── infra/                           # deployment artifacts
    ├── install.sh                   # idempotent deploy (--dry-run supported)
    ├── launcher.sh                  # WSL bootstrap — starts the server in screen
    ├── update-ip.sh                 # refresh WSL IP in mcp.json
    ├── mcp.json.snippet             # documentation reference
    ├── scripts/
    │   ├── smoke-v07.py             # live-server round-trip for every tool
    │   ├── build-architecture.py    # regenerate architecture.html from architecture.mermaid
    │   └── proof-persistence.py     # prove a session survives a server restart (v0.9)
    ├── systemd/
    │   └── bash-mcp.service         # reference unit (screen ExecStart)
    ├── hooks/
    │   ├── bash-mcp-redirect.md
    │   └── bash-mcp-redirect.js
    ├── skills/
    │   └── bash-mcp/
    │       ├── SKILL.md
    │       └── _meta.json
    └── windows/                     # Windows-side deployment helpers
        ├── BashMcp-WSL-Bootstrap.xml # Scheduled Task template
        └── install-task.ps1          # PowerShell installer for the task
```

### Add a new tool

```python
from bash_mcp import audit

@mcp.tool
def bash_my_new_tool(...) -> dict:
    """Docstring becomes the tool description for the agent."""
    ...
```

If your tool calls `subprocess.run`, wrap it in `concurrency.slot()` so it participates in the global cap:

```python
from bash_mcp.concurrency import slot as concurrency_slot

with concurrency_slot():
    subprocess.run([...])
```

### Add a denylist pattern

1. Add to `REJECT_PATTERNS` (hard) or `DANGEROUS_PATTERNS` (soft) in `src/bash_mcp/safety.py`.
2. Add a parametrized test case in `tests/test_safety.py`.
3. Update `README.md` safety table and the deployed `~/.mavis/skills/bash-mcp/SKILL.md`.
4. **Add a dated entry to the review-history block** at the top of `safety.py` listing what was added.
5. Restart the server so the new pattern is live: `screen -S bash-mcp -X quit && bash ~/projects/bash-mcp/infra/launcher.sh`.

For non-trivial gaps or batch additions, treat it as a quarterly review (see AGENTS.md).

### Adjust rotation / concurrency limits

- Change the `Environment=` lines in `infra/systemd/bash-mcp.service` (the reference unit) or export the variable before starting the launcher.
- Restart: `screen -S bash-mcp -X quit && bash ~/projects/bash-mcp/infra/launcher.sh`
- Verify via `bash_mcp_status()` that the new values are loaded.

### Test the hook manually

```bash
# Should deny (abort)
echo '{"input":{"toolName":"bash","toolArgs":{"command":"wsl -d Ubuntu -- bash -c \"echo hi\""}},"output":{}}' \
  | node /home/jbecerra/projects/bash-mcp/infra/hooks/bash-mcp-redirect.js
# => {"_abort":{"reason":"..."}}

# Should pass
echo '{"input":{"toolName":"bash","toolArgs":{"command":"git status"}},"output":{}}' \
  | node /home/jbecerra/projects/bash-mcp/infra/hooks/bash-mcp-redirect.js
# => {}

# Should pass (escape hatch)
echo '{"input":{"toolName":"bash","toolArgs":{"command":"BASH_MCP_SKIP=1 wsl echo"}},"output":{}}' \
  | node /home/jbecerra/projects/bash-mcp/infra/hooks/bash-mcp-redirect.js
# => {}
```

### Deploy to a fresh WSL machine

```bash
git clone <this-repo> /home/jbecerra/projects/bash-mcp
cd /home/jbecerra/projects/bash-mcp
./infra/install.sh
# Then from Windows PowerShell, run the printed `mavis mcp create` command.
```

## Out of scope (deferred to v0.9+)

**Already shipped:**

- ~~Stateful sessions with persistent cwd across calls~~ — **shipped in v0.6** (`bash_mcp_session_create` / `_run` / `_destroy` / `_list`). Process-lifetime only; see "What's new in v0.6.0".
- ~~Windows Scheduled Task XML for login auto-start~~ — **shipped in v0.5** at `infra/windows/BashMcp-WSL-Bootstrap.xml` + `install-task.ps1`.
- ~~**Auto-TTL / idle cleanup**~~ — **shipped in v0.7** (opt-in via `BASH_MCP_SESSION_IDLE_TIMEOUT_S`, daemon janitor thread + audit on each eviction). See "What's new in v0.7.0".
- ~~**A "denylist explainer" tool**~~ — **shipped in v0.7** as `bash_mcp_classify(command)`. Returns `{class, matched_pattern, hint, would_execute, ...}` without executing. Lets the agent self-check before sending.

**Still pending:**

- **Persistent shell state (PTY-based)** — v0.6 sessions are best-effort regex parsing of `cd` / `export` / `unset`. v0.7 could keep a real `bash` process alive per session (via `pexpect` or similar) and track shell variables, aliases, functions, `set -e` / `pipefail`, and job control. Requires a redesign of the session lifecycle and a hard cap on concurrent shells (memory + fd cost).
- ~~**Disk persistence for sessions**~~ — **shipped in v0.9** (SQLite store in `session_store.py`, restored on boot, opt-out via `BASH_MCP_SESSION_PERSIST=0`). See "What's new in v0.9.0".
- **Session snapshots** (`fork` a session at a point in time) — useful for branching workflows.
- **Cross-session env sharing** — share a named env dict across multiple sessions.
- **Web UI for browsing the audit log** — minimal Flask/FastAPI page on a separate port with filters by tool / classification / time.
- ~~**Audit log compression**~~ — **shipped in v0.7.1** via `bash_mcp_audit_read` (closes the gzip one-way archival caveat) and **completed in v0.10**, which fixed the rotation pass that destroyed the oldest backup and turned the gzip threshold on by default (1 MiB). See "What's new in v0.7.1" and "What's new in v0.10.0". Remaining: audit log shipping (Loki/CloudWatch), still deferred.
- **Audit log shipping** — Loki push API, CloudWatch Logs, or local syslog. Optional, gated by config.
- ~~**Per-project allowlists** (`.bash-mcp.toml` in repo root)~~ — **shipped in v0.8** via the `.bash-mcp.toml` schema. See **Configuration → `.bash-mcp.toml`** above. Supports both ``extend`` (merge with global) and ``replace`` (sandbox) modes.
- **A formal threat-model document** — STRIDE-style analysis of the audit log, the hook, the denylist, and the session lifecycle.
