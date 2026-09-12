# Changelog

All notable changes to **bash-mcp** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-09-12

### Added (quarterly denylist review #1)

- **Hard denylist** (6 new patterns, always rejected):
  - `find ... -delete` — recursive wipe
  - `find ... -exec rm` — recursive wipe via exec
  - Fork bomb variants: `:(){ :|& };` (broader body matching)
  - `tee /dev/{sd,hd,nvme,vd}*` — block device write via tee
  - `shred` in `/etc /var /usr /boot /bin /sbin` — unrecoverable system delete
  - `cat|cp|mv` to `/dev/sdX` — block device write via file ops

- **Soft denylist** (4 new patterns, requires `dangerous=true`):
  - `rsync --delete` — destructive sync
  - `apt remove|purge|autoremove` — package removal
  - `pip uninstall` — Python package removal
  - `npm uninstall|rm|remove -g|--global` — global npm uninstall (incl. `rm` alias and `--global` flag)

- **Denylist review history block** at the top of `safety.py` listing all reviews to date (v0.1, v0.2, v0.3, v0.4). Future quarterly reviews append a line.

### Tests

- 164 passing (was 95 in v0.3).
  - 115 safety (was 46) — added 69 parametrized cases across 10 new pattern groups + 1 review-history check
  - 27 executor (unchanged)
  - 9 errors (unchanged)
  - 6 audit_rotation (unchanged)
  - 5 concurrency (unchanged)
  - 11 e2e (unchanged)

### Backwards compatibility

No breaking changes. The 10 new patterns are additive — they catch additional dangerous commands but do not change behavior for any command that was previously SAFE or DANGEROUS.

### Deployment

```bash
systemctl --user restart bash-mcp.service
```

## [0.3.0] — 2026-09-12

### Added

- **Size-based audit log rotation**: `audit.jsonl` rotates automatically when it crosses `BASH_MCP_AUDIT_MAX_BYTES` (default 25 MB). Backups kept: `BASH_MCP_BACKUP_COUNT` (default 5). Rotation is lazy (on next write after threshold crossed), thread-safe (`_AUDIT_LOCK` covers rotation + append atomically), and fail-soft.
- **Concurrency limit**: max in-flight subprocesses capped at `BASH_MCP_MAX_CONCURRENT` (default 8) via `threading.BoundedSemaphore` at the executor boundary. Excess calls queue, never drop.
- **`bash_mcp_status` extended**:
  - `audit.max_bytes`, `audit.backup_count`, `audit.backups_present`
  - `concurrency.max_concurrent`, `concurrency.active`
- Public helpers: `audit.audit_path()`, `audit.backup_paths()`, `concurrency.active()`, `concurrency.slot()`.

### Tests

- 95 passing (was 84 in v0.2).
  - 46 safety (unchanged)
  - 27 executor (unchanged)
  - 9 errors (unchanged)
  - **6 audit_rotation (new)**: create, rotate, cap at N backups, content preservation across rotations, concurrent-write JSON safety, backup listing.
  - **5 concurrency (new)**: blocks when full, active count tracks, real subprocess bounded, `BoundedSemaphore` detects bug, default is 8.
  - 11 e2e (`test_status` expanded for new fields).

### Backwards compatibility

No breaking changes. Existing audit log entries continue to work; new writes respect the size cap. Existing tool calls continue to work; new writes are bounded by the semaphore. The `bash_mcp_status` response gains new fields (additive change — old clients ignore unknown fields).

### Deployment

```bash
./infra/install.sh
systemctl --user restart bash-mcp.service
```

## [0.2.0] — 2026-09-11

### Added

- **MIT LICENSE** file (pyproject.toml already declared MIT).
- **`bash_mcp_status` tool** — read-only health/stats. Reports uptime, audit log entry count + size, Python version, process RSS, tool list.
- **`make_error_response` helper** in server.py for testable error envelopes.
- **`hint` + `documentation` fields** in every error response (FORBIDDEN_COMMAND, DANGEROUS_COMMAND_REQUIRES_OVERRIDE, INVALID_CWD, etc.).
- **cwd allowlist validation** in executor.py with Windows-style path conversion (`C:\foo` → `/mnt/c/foo`).
- **Versioned infra/ tree**: `infra/{systemd,hooks,skills}/`, `infra/install.sh`, `infra/mcp.json.snippet`. Idempotent deploy to fresh WSL/Windows.

### Changed

- Project layout: deployment files moved from repo root to `infra/` (via `git mv`).
- README + AGENTS.md updated to reference `infra/` paths.

### Tests

- 84 passing (was 55 in v0.1).
- 27 executor (was 9) — added 18 for cwd allowlist + Windows path conversion.
- 9 errors (new file `tests/test_errors.py`).
- 11 e2e (was 9) — added status + invalid_cwd hint assertions.

## [0.1.0] — 2026-09-11

### Added

- Initial release: WSL bash executor MCP server.
- Streamable-http transport on port `54321` (IANA dynamic range, defensive against collision).
- Tools:
  - `bash_run_command` (with hard/soft denylist, timeouts, output truncation, audit logging)
  - `bash_check_env`
  - `bash_list_binaries`
  - `bash_which`
- Hard denylist: `rm -rf /`, `dd of=/dev/{sd,hd,nvme,vd}*`, fork bomb, `mkfs`, `curl|bash`, `wget|bash`, `sudo rm`.
- Soft denylist: `sudo`, `kill -9`, `systemctl stop/disable/mask`, `git push --force`, `pip install`, etc. (requires `dangerous=true` to bypass).
- JSONL audit log at `~/.local/share/bash-mcp/audit.jsonl`.
- systemd user service `bash-mcp.service` (Type=forking, screen-based, `Restart=on-failure`, `RestartSec=5`).
- PreToolUse hook in MiniMax Code denies `wsl -d ... -- bash -c "..."` shell calls.
- Skill at `~/.mavis/skills/bash-mcp/SKILL.md` with trigger keywords.
- `mcp.json` entry with prescriptive description.

### Tests

- 55 passing.
  - 46 safety (parametrized denylist cases)
  - 9 executor
  - 11 e2e (live server round-trip)

### Notes

- IANA dynamic port range (49152–65535) chosen for collision avoidance.
- Windows-side mcp.json + hook + skill deployment is one-time setup per machine.
- mavis runtime registry requires manual `mavis mcp create` from PowerShell (cannot be automated from WSL).
