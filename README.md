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

## What's new in v0.3.0

- **Audit log rotation** — `audit.jsonl` rotates at 25 MB (configurable) keeping 5 backups (`audit.jsonl.{1..5}`). Lazy + thread-safe.
- **Concurrency limit** — max 8 concurrent subprocesses via `threading.BoundedSemaphore` at the executor boundary. Excess calls queue, never drop.
- **`bash_mcp_status` extended** with `audit.{max_bytes, backup_count, backups_present}` and `concurrency.{max_concurrent, active}` fields.
- 95 tests passing (up from 84).

See [CHANGELOG.md](CHANGELOG.md) for full history.

## Tools

| Tool | Purpose |
|---|---|
| `bash-mcp_run_command(command, cwd?, timeout_ms?, env?, dangerous?)` | Execute bash. Returns structured output. |
| `bash-mcp_check_env()` | OS, kernel, Python, uv, PATH entries. |
| `bash-mcp_list_binaries()` | ~37 well-known tools with `{path, exists, version?}`. |
| `bash-mcp_which(name)` | Resolve a single binary. |
| `bash-mcp_status()` | Service health, audit stats + rotation config, concurrency, uptime, tool list. |

All tools are namespaced as `bash-mcp_*` in MiniMax Code.

## Quick start (for the agent)

```javascript
// Discover environment
bash-mcp_check_env()

// Check if a tool is installed
bash-mcp_list_binaries()       // batch
bash-mcp_which({name: "rg"})   // single

// Run a command
bash-mcp_run_command({command: "ls -lah /tmp"})

// Run with custom cwd and timeout
bash-mcp_run_command({command: "pytest -xvs", cwd: "/home/jbecerra/projects/myapp", timeout_ms: 120000})

// Bypass the SOFT denylist
bash-mcp_run_command({command: "sudo systemctl restart nginx", dangerous: true})

// Verify service health (audit + concurrency state)
bash-mcp_status()
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

Streamable-http transport. Stateless per call.

## Safety

| Class | Trigger | Behavior |
|---|---|---|
| `safe` | No denylist match | Execute |
| `dangerous` | Soft pattern (sudo, kill -9, pip install, etc.) | Reject unless `dangerous=true` |
| `reject` | Hard pattern (rm -rf /, dd of=/dev/sd*, fork bomb, mkfs) | Always reject |

Hard patterns are never bypassed, even with `dangerous=true`. See `src/bash_mcp/safety.py` for the full regex lists.

Every error response includes `hint` (actionable next step) and `documentation` (path to the skill) — the agent does not need to parse free text to recover.

`cwd` is allowlist-validated: must resolve under `$HOME`, `/tmp`, `/home`, `/mnt/c/Users/jesus`, or `/var/tmp`. Windows-style paths (`C:\foo`) are auto-converted to `/mnt/c/foo`.

## Configuration

All via environment variables. Set in `~/.config/systemd/user/bash-mcp.service` (Environment= lines), or before running locally.

| Variable | Default | Purpose |
|---|---|---|
| `FASTMCP_SERVER_HOST` | `0.0.0.0` | Bind address |
| `FASTMCP_SERVER_PORT` | `54321` | Bind port (IANA dynamic range) |
| `BASH_MCP_AUDIT_MAX_BYTES` | `26214400` (25 MB) | Audit log rotation threshold |
| `BASH_MCP_AUDIT_BACKUP_COUNT` | `5` | Audit log backups kept |
| `BASH_MCP_MAX_CONCURRENT` | `8` | Max in-flight subprocesses |

## Deploying

`infra/install.sh` deploys everything to a fresh WSL/Windows setup:

- systemd unit → `~/.config/systemd/user/bash-mcp.service`
- PreToolUse hook → `~/.minimax/agents/mavis/hooks/bash-mcp-redirect.md`
- Skill → `~/.mavis/skills/bash-mcp/`
- mcp.json entry → `~/.minimax/mcp/mcp.json`

The script also prints the one `mavis mcp create` command that must be
run from Windows PowerShell (the runtime registry is Windows-side).

```bash
./infra/install.sh
```

## Operations

### Start / stop / restart

```bash
systemctl --user start bash-mcp.service
systemctl --user stop bash-mcp.service
systemctl --user restart bash-mcp.service
systemctl --user status bash-mcp.service

# Logs (via screen session)
screen -r bash-mcp

# Or via journalctl
journalctl --user -u bash-mcp.service -f
```

### Audit log (rotation in v0.3+)

The audit log at `~/.local/share/bash-mcp/audit.jsonl` rotates automatically when it crosses `BASH_MCP_AUDIT_MAX_BYTES` (default 25 MB).

```bash
# Tail the current log
tail -f ~/.local/share/bash-mcp/audit.jsonl

# Pretty-print last 5 entries
tail -5 ~/.local/share/bash-mcp/audit.jsonl | jq .

# Check current size + rotation config
bash-mcp_status  # → audit.size_bytes, audit.max_bytes, audit.backup_count, audit.backups_present

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
bash-mcp_status  # → concurrency.max_concurrent, concurrency.active
```

If you make 20 parallel tool calls, 8 run, 12 queue. The semaphore is at the executor boundary, so `bash_run_command`, `bash_check_env`'s version probes, and any future subprocess-using tool all share the same cap.

### Health check (no SSH/curl needed)

```bash
# Via MiniMax Code (recommended):
bash-mcp_status

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

The plan was to extend `semantic-memory-launcher.sh`, but to respect the parallel-infra principle we ship `infra/launcher.sh` instead. To enable on Windows login:

```powershell
# As admin, register a new Scheduled Task similar to SemanticMemory-WSL-Bootstrap:
schtasks /Create /TN BashMcp-WSL-Bootstrap /XML C:\path\to\bash-mcp-launcher-task.xml
```

(The XML template is not yet checked in; clone the semantic-memory one and adjust the script path.)

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

**95 tests** cover:

- 46 parametrized denylist cases (test_safety.py)
- 27 executor cases including allowlist + Windows→WSL path conversion (test_executor.py)
- 9 error envelope cases (test_errors.py)
- 6 audit log rotation cases (test_audit_rotation.py) — v0.3
- 5 concurrency limit cases (test_concurrency.py) — v0.3
- 11 e2e tests against the live server (test_e2e.py)

### Project layout

```
bash-mcp/
├── AGENTS.md                        # project-scoped rules for future agents
├── CHANGELOG.md                     # version history
├── LICENSE                          # MIT
├── README.md                        # this file
├── pyproject.toml                   # uv-managed, FastMCP 2.7.0
├── uv.lock
├── src/bash_mcp/                    # server code
│   ├── server.py                    # FastMCP + 6 tools
│   ├── executor.py                  # subprocess wrapper, timeout, truncation, cwd allowlist, concurrency slot
│   ├── safety.py                    # classify(command)
│   ├── audit.py                     # JSONL append-only logger + size-based rotation
│   ├── discovery.py                 # which / list_binaries (thread-pool)
│   └── concurrency.py               # BoundedSemaphore wrapper (v0.3)
├── tests/                           # 95 tests
│   ├── test_safety.py               # 46 parametrized denylist cases
│   ├── test_executor.py             # 27 subprocess + cwd allowlist
│   ├── test_errors.py               # 9 error envelope shape + hints
│   ├── test_audit_rotation.py       # 6 rotation cases (v0.3)
│   ├── test_concurrency.py          # 5 concurrency limit cases (v0.3)
│   └── test_e2e.py                  # 11 live-server round-trips
└── infra/                           # deployment artifacts
    ├── install.sh                   # idempotent deploy
    ├── launcher.sh                  # WSL bootstrap (parallel to semantic-memory-launcher.sh)
    ├── update-ip.sh                 # refresh WSL IP in mcp.json
    ├── mcp.json.snippet             # documentation reference
    ├── systemd/
    │   └── bash-mcp.service
    ├── hooks/
    │   ├── bash-mcp-redirect.md
    │   └── bash-mcp-redirect.js
    └── skills/
        └── bash-mcp/
            ├── SKILL.md
            └── _meta.json
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
3. Update `README.md` safety table.

### Adjust rotation / concurrency limits

- Change the systemd unit (`infra/systemd/bash-mcp.service`), `Environment=BASH_MCP_AUDIT_MAX_BYTES=...`, etc.
- `systemctl --user daemon-reload && systemctl --user restart bash-mcp.service`
- Verify via `bash-mcp_status()` that the new values are loaded.

### Test the hook manually

```bash
# Should deny (abort)
echo '{"input":{"toolName":"bash","toolArgs":{"command":"wsl -d Ubuntu -- bash -c \"echo hi\""}},"output":{}}' \
  | wsl -d Ubuntu-22.04 -- node /home/jbecerra/projects/bash-mcp/infra/hooks/bash-mcp-redirect.js
# => {"_abort":{"reason":"..."}}

# Should pass
echo '{"input":{"toolName":"bash","toolArgs":{"command":"git status"}},"output":{}}' \
  | wsl -d Ubuntu-22.04 -- node /home/jbecerra/projects/bash-mcp/infra/hooks/bash-mcp-redirect.js
# => {}

# Should pass (escape hatch)
echo '{"input":{"toolName":"bash","toolArgs":{"command":"BASH_MCP_SKIP=1 wsl echo"}},"output":{}}' \
  | wsl -d Ubuntu-22.04 -- node /home/jbecerra/projects/bash-mcp/infra/hooks/bash-mcp-redirect.js
# => {}
```

### Deploy to a fresh WSL machine

```bash
git clone <this-repo> /home/jbecerra/projects/bash-mcp
cd /home/jbecerra/projects/bash-mcp
./infra/install.sh
# Then from Windows PowerShell, run the printed `mavis mcp create` command.
```

## Out of scope (deferred to v0.4+)

- Stateful sessions with persistent cwd across calls (v0.4).
- PTY for streaming/interactive commands (v0.4).
- Web UI for browsing the audit log (v0.5+).
- Audit log compression with gzip on rotation (if disk > 500 MB).
- Audit log shipping to external sink (Loki, CloudWatch).
- Windows Scheduled Task XML for login auto-start (deferred from v0.3).
- More denylist patterns (review quarterly).
