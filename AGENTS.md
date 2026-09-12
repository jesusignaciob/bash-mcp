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
├── server.py       # FastMCP server + 6 tools (@mcp.tool)
├── executor.py     # subprocess.run wrapper, timeout, truncation, cwd allowlist, concurrency slot
├── safety.py       # classify(command) -> Class.{SAFE,DANGEROUS,REJECT}
├── audit.py        # JSONL append-only logger + size-based rotation (v0.3)
├── discovery.py    # which / list_binaries (thread-pool based)
└── concurrency.py  # threading.BoundedSemaphore wrapper (v0.3)
tests/
├── test_safety.py            # 46 parametrized denylist cases
├── test_executor.py          # 27 cases: subprocess + cwd allowlist + Windows→WSL
├── test_errors.py            # 9 cases: error envelope shape + hint text
├── test_audit_rotation.py    # 6 cases: rotation + concurrency (v0.3)
├── test_concurrency.py       # 5 cases: semaphore behavior (v0.3)
└── test_e2e.py               # 11 cases: live server round-trip
infra/                                  # deployment artifacts (v0.2+)
├── install.sh                          # idempotent deploy to fresh WSL/Windows
├── launcher.sh                         # WSL bootstrap (parallel to semantic-memory-launcher.sh)
├── update-ip.sh                        # refresh WSL IP in mcp.json (parallel)
├── mcp.json.snippet                    # documentation reference
├── systemd/bash-mcp.service            # canonical unit
├── hooks/
│   ├── bash-mcp-redirect.md            # PreToolUse hook (deployed to ~/.minimax/agents/mavis/hooks/)
│   └── bash-mcp-redirect.js            # Node script invoked by the hook
├── skills/bash-mcp/
│   ├── SKILL.md                        # deployed to ~/.mavis/skills/bash-mcp/
│   └── _meta.json
└── windows/                            # Windows-side deployment helpers
    ├── BashMcp-WSL-Bootstrap.xml       # Scheduled Task template (Logon trigger)
    └── install-task.ps1                # PowerShell installer for the Scheduled Task
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
- **Adding a denylist pattern** (incremental change, single pattern):
  1. Add to `REJECT_PATTERNS` (hard) or `DANGEROUS_PATTERNS` (soft) in `src/bash_mcp/safety.py`.
  2. Add a parametrized test case in `tests/test_safety.py` (≥3 cases: positive + negative to catch false positives).
  3. Update the safety table in `README.md` and the deployed `~/.mavis/skills/bash-mcp/SKILL.md` (Safety Model section).
  4. **Append a dated line** to the review-history block at the top of `safety.py` (see quarterly review below).
  5. Run tests, then `systemctl --user restart bash-mcp.service` so the new pattern is live.

- **Quarterly denylist review** (batch additions, ~every 90 days):
  - Open the review-history block at the top of `safety.py`. The previous review is the starting point.
  - Identify gaps: realistic dangerous commands an agent might emit but that aren't blocked. Categories to scan: block-device destruction, fork bombs + variants, recursive delete via find, package removal, destructive sync, supply-chain attacks.
  - Add the patterns + tests (same checklist as incremental above).
  - Append a single dated line to the review-history block summarizing what was added.
  - Update README + SKILL.md.
  - Commit as a single release (e.g. `v0.4: quarterly denylist review #1`).
- **Changing the port**: update BOTH `infra/systemd/bash-mcp.service` `Environment=FASTMCP_SERVER_PORT=` AND `infra/update-ip.sh` `sed` pattern AND `mcp.json` `url` field. Use the [IANA dynamic range](https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.xml) (49152-65535) to minimize collision risk.
- **Changing the WSL IP**: run `./infra/update-ip.sh`. It rewrites only the `bash-mcp` URL in `mcp.json` (and Cursor's, if present). Does not touch `semantic-memory` URLs.
- **Deploying to a fresh WSL/Windows setup**: run `./infra/install.sh`. It idempotently copies the systemd unit, hook, skill, and mcp.json entry, and prints the manual `mavis mcp create` command for Windows PowerShell.
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
3. If they're debugging the hook → test it manually: `echo '<json>' | wsl -d Ubuntu-22.04 -- node /home/jbecerra/projects/bash-mcp/infra/hooks/bash-mcp-redirect.js`. Use `BASH_MCP_SKIP=1` prefix for pass-through tests.
4. If the WSL IP changed → run `./infra/update-ip.sh`.
5. If the service is down → `systemctl --user status bash-mcp.service`, then `systemctl --user restart bash-mcp.service`.
