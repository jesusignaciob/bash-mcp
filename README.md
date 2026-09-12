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

## Tools

| Tool | Purpose |
|---|---|
| `bash-mcp_run_command(command, cwd?, timeout_ms?, env?, dangerous?)` | Execute bash. Returns structured output. |
| `bash-mcp_check_env()` | OS, kernel, Python, uv, PATH entries. |
| `bash-mcp_list_binaries()` | ~37 well-known tools with `{path, exists, version?}`. |
| `bash-mcp_which(name)` | Resolve a single binary. |

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
```

## ⚠️ DO NOT use `wsl -d ... -- bash -c "..."`

A `PreToolUse` hook in MiniMax Code **denies** any shell call whose command starts with `wsl(\.exe)?\s`. If you trigger it, you'll see an abort reason pointing you here.

**Escape hatch** (for genuine testing): prefix the command with `BASH_MCP_SKIP=1`.

## Architecture

```
MiniMax Code ──HTTP POST──> bash-mcp server (WSL, port 54321)
                            │
                            ├─ classify(command) → safe / dangerous / reject
                            ├─ subprocess.run([bash, "-lc", command], ...)
                            ├─ truncate stdout/stderr to 50KB
                            └─ audit.log(...) → ~/.local/share/bash-mcp/audit.jsonl
```

Streamable-http transport. Stateless per call.

## Safety

| Class | Trigger | Behavior |
|---|---|---|
| `safe` | No denylist match | Execute |
| `dangerous` | Soft pattern (sudo, kill -9, pip install, etc.) | Reject unless `dangerous=true` |
| `reject` | Hard pattern (rm -rf /, dd of=/dev/sd*, fork bomb, mkfs) | Always reject |

Hard patterns are never bypassed, even with `dangerous=true`. See `src/bash_mcp/safety.py` for the full regex lists.

## Operations

### Start / stop / restart

```bash
# Start
systemctl --user start bash-mcp.service

# Stop
systemctl --user stop bash-mcp.service

# Restart
systemctl --user restart bash-mcp.service

# Status
systemctl --user status bash-mcp.service

# Logs (via screen session)
screen -r bash-mcp

# Or via journalctl
journalctl --user -u bash-mcp.service -f
```

### Audit log

```bash
# Tail the JSONL audit log
tail -f ~/.local/share/bash-mcp/audit.jsonl

# Pretty-print last 5 entries
tail -5 ~/.local/share/bash-mcp/audit.jsonl | jq .
```

### WSL IP changed (after reboot)

```bash
cd /home/jbecerra/projects/bash-mcp
./update-ip.sh
```

This rewrites only the `bash-mcp` URL in `~/.minimax/mcp/mcp.json` (and `~/.cursor/mcp.json` if present). Does not touch `semantic-memory`.

### Bootstrap on Windows login

The plan was to extend `semantic-memory-launcher.sh`, but to respect the parallel-infra principle we ship `bash-mcp/launcher.sh` instead. To enable on Windows login:

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

46 tests cover:
- Hard denylist (one assertion per regex + path exemptions)
- Soft denylist
- Safe baseline
- Executor: echo, exit codes, timeout, env, cwd, error cases

### Add a new tool

```python
from bash_mcp import audit

@mcp.tool
def bash_my_new_tool(...) -> dict:
    """Docstring becomes the tool description for the agent."""
    ...
```

### Add a denylist pattern

1. Add to `REJECT_PATTERNS` (hard) or `DANGEROUS_PATTERNS` (soft) in `src/bash_mcp/safety.py`.
2. Add a parametrized test case in `tests/test_safety.py`.
3. Update `README.md` safety table.

### Test the hook manually

```bash
# Should deny (abort)
echo '{"input":{"toolName":"bash","toolArgs":{"command":"wsl -d Ubuntu -- bash -c \"echo hi\""}},"output":{}}' \
  | wsl -d Ubuntu-22.04 -- node /home/jbecerra/projects/bash-mcp/hooks/bash-mcp-redirect.js
# => {"_abort":{"reason":"..."}}

# Should pass
echo '{"input":{"toolName":"bash","toolArgs":{"command":"git status"}},"output":{}}' \
  | wsl -d Ubuntu-22.04 -- node /home/jbecerra/projects/bash-mcp/hooks/bash-mcp-redirect.js
# => {}

# Should pass (escape hatch)
echo '{"input":{"toolName":"bash","toolArgs":{"command":"BASH_MCP_SKIP=1 wsl echo"}},"output":{}}' \
  | wsl -d Ubuntu-22.04 -- node /home/jbecerra/projects/bash-mcp/hooks/bash-mcp-redirect.js
# => {}
```

## Files of interest

| Path | Purpose |
|---|---|
| `src/bash_mcp/server.py` | FastMCP server + 4 tools |
| `src/bash_mcp/executor.py` | subprocess wrapper, timeout, truncation |
| `src/bash_mcp/safety.py` | classify(command) |
| `src/bash_mcp/audit.py` | JSONL audit logger |
| `src/bash_mcp/discovery.py` | which / list_binaries |
| `tests/test_safety.py` | 46 denylist assertions |
| `tests/test_executor.py` | subprocess behavior |
| `systemd/bash-mcp.service` | systemd unit (canonical) |
| `launcher.sh` | WSL bootstrap (parallel to semantic-memory-launcher) |
| `update-ip.sh` | refresh WSL IP in mcp.json |
| `hooks/bash-mcp-redirect.js` | Node script invoked by PreToolUse hook |
| `AGENTS.md` | project-scoped rules for future agents |

## Out of scope (deferred to v2)

- Stateful sessions with persistent cwd/env across calls.
- PTY for interactive / streaming commands.
- Per-call UI confirmation (instead we use the explicit `dangerous` flag).
- Windows-host command execution (only WSL for now).
- macOS / other distros (WSL Ubuntu 22.04 only).
