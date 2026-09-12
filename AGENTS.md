# AGENTS.md — bash-mcp project

This file applies to any agent opening **this project** (`/home/jbecerra/projects/bash-mcp/`) for maintenance, extension, or debugging.

## Project: bash-mcp

A small MCP server that exposes WSL bash as a structured tool to MiniMax Code. Stateless executor with denylist safety, JSONL audit, and systemd lifecycle.

## Stack

- Python 3.10 + FastMCP 2.7.0 (mirror `mcp-server-qdrant`).
- uv-managed (`uv sync` to install; `uv run python -m bash_mcp.server` to run).
- Streamable-http on port `54321` (IANA dynamic range — defensive port choice to avoid collision with common dev tools).
- systemd user service `bash-mcp.service` mirrors `semantic-mcp.service` (Type=forking, screen-based, Restart=on-failure, StartLimitBurst=10/300s).

## Architecture

```
src/bash_mcp/
├── server.py     # FastMCP server + 4 tools (@mcp.tool)
├── executor.py   # subprocess.run wrapper, timeout, truncation
├── safety.py     # classify(command) -> Class.{SAFE,DANGEROUS,REJECT}
├── audit.py      # JSONL append-only logger
└── discovery.py  # which / list_binaries (thread-pool based)
tests/
├── test_safety.py    # One assertion per denylist regex + safe baselines
└── test_executor.py  # echo, timeout, exit codes, env, cwd
systemd/bash-mcp.service  # canonical unit (copy to ~/.config/systemd/user/)
launcher.sh              # WSL bootstrap (parallel to semantic-memory-launcher.sh)
update-ip.sh             # refresh WSL IP in mcp.json (parallel)
hooks/bash-mcp-redirect.js  # Node script used by PreToolUse hook
```

## Running

```bash
source ~/.local/bin/env
export PATH="$HOME/.local/bin:$PATH"
cd /home/jbecerra/projects/bash-mcp
uv run python -m bash_mcp.server --transport streamable-http --host 0.0.0.0 --port 54321
```

Test:
```bash
uv run pytest tests/ -v
```

## Conventions for future edits

- **Adding a tool**: register via `@mcp.tool` in `server.py`. Read-only tools don't need audit. Mutating tools should write to `audit.log(...)` and respect `Class.REJECT` (hard floor).
- **Adding a denylist pattern**: add to `safety.py` AND add a parametrized test case in `tests/test_safety.py`. REJECT patterns cannot be bypassed; DANGEROUS patterns require `dangerous=true`.
- **Changing the port**: update BOTH `systemd/bash-mcp.service` `Environment=FASTMCP_SERVER_PORT=` AND `update-ip.sh` `sed` pattern AND `mcp.json` `url` field. Use the [IANA dynamic range](https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.xml) (49152-65535) to minimize collision risk.
- **Changing the WSL IP**: run `./update-ip.sh`. It rewrites only the `bash-mcp` URL in `mcp.json` (and Cursor's, if present). Does not touch `semantic-memory` URLs.
- **Adding an enforcement layer**: update `C:\Users\jesus\.mavis\skills\bash-mcp\SKILL.md` AND `C:\Users\jesus\.minimax\agents\mavis\hooks\bash-mcp-redirect.md` AND the `description` field in `mcp.json` — three places that must stay in sync.

## Don't

- Do NOT modify `semantic-mcp.service`, `semantic-memory-launcher.sh`, or `semantic-memory/update-ip.sh`. Bash-mcp is a parallel infra with its own artifacts.
- Do NOT change the hook's `matcher` from `^bash$` without coordinating with the user — it could disable the entire enforcement layer.
- Do NOT add env-var values to the audit log (security: secrets in env could leak). Audit logs only env keys, never values.
- Do NOT lower the hard denylist. It is the safety floor; lowering it removes the only guarantee that `rm -rf /` cannot execute.
- Do NOT remove the `BASH_MCP_SKIP=1` escape hatch — it's how the hook is tested without self-interception.

## When the user asks about bash-mcp

1. If they're asking how to install / use it → point them to `README.md` (Phase 4 deliverable) and the skill at `C:\Users\jesus\.mavis\skills\bash-mcp\SKILL.md`.
2. If they're asking why a command was rejected → check the audit log: `tail ~/.local/share/bash-mcp/audit.jsonl`. The `matched_pattern` field tells which regex fired.
3. If they're debugging the hook → test it manually: `echo '<json>' | wsl -d Ubuntu-22.04 -- node /home/jbecerra/projects/bash-mcp/hooks/bash-mcp-redirect.js`. Use `BASH_MCP_SKIP=1` prefix for pass-through tests.
4. If the WSL IP changed → run `./update-ip.sh`.
5. If the service is down → `systemctl --user status bash-mcp.service`, then `systemctl --user restart bash-mcp.service`.
