---
name: bash-mcp
description: "WSL bash executor MCP — REQUIRED way to run WSL commands from the agent. Triggers on 'wsl', 'bash', 'shell', 'command', 'exec', 'terminal', 'ubuntu', 'linux', or any prompt that requires running shell commands inside WSL. Use bash-mcp_run_command instead of `wsl -d ... -- bash -c \"...\"` from PowerShell — the latter has quoting, UTF-16, and PATH bugs. Other tools: bash_check_env (OS/PATH info), bash_list_binaries (known tools), bash_which (resolve binary)."
license: MIT
metadata:
  version: "1.0"
  category: tools
---

# bash-mcp — WSL Bash Executor

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

### `bash-mcp_status()`

Read-only health check. Returns `{service, version, uptime_seconds, start_time, audit: {path, entries, size_bytes}, python_version, process: {pid, rss_bytes}, tools: [...]}`. Use this to verify the service is up without SSH/tail logs.

## Safety Model

- **HARD denylist** — always rejected (cannot be bypassed):
  - `rm -rf /` (except `/tmp`, `/home`, `/Users`)
  - `dd of=/dev/{sd,hd,nvme,vd}*`
  - `:(){ :|:& };:` (fork bomb)
  - `mkfs /dev/*`
  - `> /dev/{sd,hd,nvme,vd}*`
  - `chmod -R NNN /` (except `/tmp`, `/home`, `/Users`)
  - `curl ... | bash`, `wget ... | bash`
  - `sudo rm`

- **SOFT denylist** — requires `dangerous=true`:
  - `sudo`, `kill -9`, `kill -SIGKILL`
  - `systemctl stop|disable|mask`
  - `git push --force`, `git push -f`
  - `pip install`, `npm install -g`, `apt(-get) install`
  - `chown -R`
  - `chmod NNN /`
  - `> /etc/`

- All calls are audit-logged to `~/.local/share/bash-mcp/audit.jsonl` (JSONL).
- Audit log is append-only; env values are NOT logged (only env var keys).

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

// Verify service health
bash-mcp_status()
```

## Operational Notes

- Server runs in WSL via systemd (`systemctl --user status bash-mcp.service`).
- Listens on `http://172.20.50.88:54321/mcp/` (WSL IP changes on reboot; run `./infra/update-ip.sh` from the repo root to refresh).
- Logs: `journalctl --user -u bash-mcp.service -f` or `screen -r bash-mcp`.
- Audit: `tail -f ~/.local/share/bash-mcp/audit.jsonl`.
- Deploy a fresh WSL/Windows setup: `./infra/install.sh` from the repo root.
