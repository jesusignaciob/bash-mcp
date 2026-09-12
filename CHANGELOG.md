# Changelog

All notable changes to **bash-mcp** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] — 2026-09-12

### Added

- **Stateful sessions** (`src/bash_mcp/sessions.py`) — four new tools that keep a server-side `cwd` and accumulated `env` dict across calls:
  - `bash_mcp_session_create(name?, cwd?)` → `{session_id, name, cwd, created_at, last_used_at, env_count, audit_id}`
  - `bash_mcp_session_run(session_id, command, timeout_ms?, dangerous?)` → execution result + persisted `cwd` / `env_keys`
  - `bash_mcp_session_destroy(session_id)` → `{ok, session_id, freed_env_vars, audit_id}`
  - `bash_mcp_session_list()` → array of `{session_id, name, cwd, last_used_at, env_count, env_keys}` (most-recently-used first)
- **State parsing** — best-effort regex parser extracts top-level `cd PATH`, `export VAR=value`, `unset VAR` from command text after a successful run (`exit_code == 0`). Supports `;` / `&&` / `&` chains, single / double / bareword quoted values, and `~` expansion. Multi-segment chains: last `cd` wins.
- **Per-session env cap** — default 256 vars, configurable via `BASH_MCP_SESSION_MAX_ENV_VARS`. Silently drops new distinct vars past the cap; overwriting existing keys is allowed.
- **`bash_mcp_status` extended** with `sessions.{active, max_env_per_session}` and the 4 new tool names in the `tools` array.
- **Audit integration** — `bash_mcp_session_run` writes the same shape as `bash_run_command` plus `args.session_id`. Create / destroy write compact entries via `audit.log(...)`.
- **Version bump** — `__version__` in `src/bash_mcp/__init__.py` corrected from the stale `0.1.0` to `0.6.0` (it had been frozen at v0.1 across all prior releases).
- **`executor.__all__`** — explicit re-export list added; `_convert_windows_path` and `_is_under_allowed_root` are now deliberately shared with `sessions.py` (allowlist semantics are part of the documented safety contract).

### Tests

- **220 passing** (up from 164).
- New `tests/test_sessions.py` — 51 unit tests covering:
  - create / get / destroy / list (15 cases, including Windows path conversion and invalid-cwd rejection)
  - `apply_state_update` for `cd` / `export` / `unset` (16 cases, including chained commands, quoted values, failed-command rollback, env cap, overwrite-without-counting, no-op unset)
  - 4 server tools called directly via the FastMCP `FunctionTool.fn` accessor (16 cases, including denylist parity, audit_id presence, SESSION_NOT_FOUND envelopes)
  - isolation between concurrent sessions + concurrency limit interaction
  - process-lifetime verified by spawning a fresh subprocess
- `tests/test_e2e.py` — 5 new live-server tests + existing `test_status` extended with v0.6 assertions (`sessions` block + new tool names + version `0.6.0`).

### Backwards compatibility

- `bash_run_command`, `bash_check_env`, `bash_list_binaries`, `bash_which`, `bash_mcp_status`, `echo` — **unchanged**. Zero regressions in existing 164 tests.
- Sessions are strictly opt-in. No existing call path is affected.
- `executor.run` signature unchanged.

### Limitations (documented)

- **Process-lifetime** — sessions are lost on server restart. No auto-TTL. Caller must invoke `bash_mcp_session_destroy` to free memory.
- **Parser scope** — only top-level statements on simple command chains. Heredocs, `$(...)`, function bodies, multi-line `\` continuations are NOT tracked (the subprocess still runs correctly; only state persistence is skipped).
- **Quoted export values are stored literally** — no shell expansion. `export X="$HOME"` records the literal `$HOME`; the subprocess in the same call still sees the expanded value via `bash -lc`.
- **Session IDs are 34-character UUID4 hex** (`s_<32 hex>`). No auth in v0.6 (same trust model as the rest of the server).

### Deployment

```bash
cd /home/jbecerra/projects/bash-mcp
git pull            # or this commit, once pushed
systemctl --user restart bash-mcp.service   # picks up new session tools
# verify:
curl http://127.0.0.1:54321/mcp/ ...   # see bash_mcp_status output
```

No config changes required. New env var `BASH_MCP_SESSION_MAX_ENV_VARS` is optional.

## [0.5.0] — 2026-09-12

### Added

- **`infra/install.sh --dry-run` flag** — prints what would happen without making changes. Useful for preview before first deploy or after a repo update.
- **`infra/install.sh -h/--help`** — usage info.

### Fixed (found during end-to-end install.sh test)

- **Hook JS BOM-stripping** (`infra/hooks/bash-mcp-redirect.js`) — when invoked via `wsl -d ... -- node script.js`, the harness's pipe may prepend a UTF-8 BOM. The hook now strips leading BOM and whitespace before `JSON.parse`, so the abort path fires correctly.
- **Hook MD path** (`~/.minimax/agents/mavis/hooks/bash-mcp-redirect.md`) — was pointing to `/home/jbecerra/projects/bash-mcp/hooks/bash-mcp-redirect.js` (the v0.1 path) instead of `/home/jbecerra/projects/bash-mcp/infra/hooks/bash-mcp-redirect.js` (the v0.2 path). Latent bug from v0.2; never triggered because the hook had not been invoked in production. Fixed by re-running `infra/install.sh`.
- **`install.sh` mcp.json edit step** — `python3 - <<PYEOF` does not expand bash heredoc variables when piping to `python3 -`. Now passes `WSL_IP` and `MCP_JSON` via env vars (`BASH_MCP_MCP_JSON` / `BASH_MCP_WSL_IP`) instead.

### Tests

- 164 passing (unchanged from v0.4).
- All three hook scenarios verified end-to-end: `wsl ...` (abort), `git status` (pass-through), `BASH_MCP_SKIP=1 wsl` (escape hatch).

### Backwards compatibility

No breaking changes. `--dry-run` is opt-in; default behavior is unchanged.

### Deployment

```bash
./infra/install.sh --dry-run    # preview first
./infra/install.sh              # apply
systemctl --user restart bash-mcp.service
```

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
