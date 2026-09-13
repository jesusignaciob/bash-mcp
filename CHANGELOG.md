# Changelog

All notable changes to **bash-mcp** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.8.0] — 2026-09-13

### Added

- **Per-project cwd allowlists via `.bash-mcp.toml`** — projects can drop a TOML file at the repo root (or any ancestor) to override the global `ALLOWED_CWD_ROOTS` for calls whose cwd lives under that directory. Two modes:
  - `extend` (default) — ADD the project's `allowed_roots` to the global list. Useful for projects exposing worktrees/build dirs outside the global allowlist.
  - `replace` — REPLACE the global list with the project's roots. Useful for sandboxed environments where the global list is too permissive (empty list = deliberate lockout).
- **New module `src/bash_mcp/project_allowlist.py`** — walk-up search for `.bash-mcp.toml` (stops at `$HOME` or filesystem root; 32-hop safety belt), TOML parse with `tomllib` (3.11+) / `tomli` (3.10), mode resolver, process-lifetime cache keyed on `(start_dir, mtime_ns)` so editing the TOML invalidates the entry automatically. Fail-soft on malformed TOML / unknown mode / non-list `allowed_roots` (stderr warning + fall back to global).
- **`executor._is_under_allowed_root(cwd, effective_roots=None)`** — now accepts an optional `effective_roots` tuple; defaults to `ALLOWED_CWD_ROOTS`. `executor.run()` and `sessions._resolve_cwd()` both call `project_allowlist.effective_allowed_roots(cwd, ALLOWED_CWD_ROOTS)` before the check. Zero behavior change when no `.bash-mcp.toml` is found.
- **`server._hint_for_invalid_cwd`** — mentions the project allowlist source + roots when one is in effect (so `INVALID_CWD` errors point the user at the project config, not just the global allowlist).
- **Version bump** — `__version__` in `src/bash_mcp/__init__.py` bumped from `0.7.1` to `0.8.0`.
- **`tomli>=2.0` direct dependency** — promoted from transitive to direct in `pyproject.toml` (still conditional on Python < 3.11; `tomllib` is stdlib on 3.11+).

### Tests

- **281 passing** (up from 265 before v0.8; 11 unit + 3 executor + 2 sessions added; e2e 24 skipped when no live server).
- New `tests/test_project_allowlist.py` — 11 unit tests covering walk-up, negative cache, mtime invalidation, extend/replace/empty-lockout, and fail-soft on malformed TOML / unknown mode / non-list `allowed_roots`.
- `tests/test_executor.py` — 3 new tests (extend allows, replace drops global, no-TOML falls back to global).
- `tests/test_sessions.py` — 2 new tests (session_create with project allowlist, session_create rejected by replace-mode lockout).
- `infra/scripts/smoke-v07.py` — 1 new section (~3 cases) for per-project allowlist live-server round-trip.

### Backwards compatibility

- Zero behavior change when no `.bash-mcp.toml` is present (the most common case).
- All v0.7.1 callers see zero behavior change.
- 12 MCP tools — no new tool added (v0.8 is config-only).
- `bash_mcp_status` output unchanged.
- No new environment variables; no install.sh change.

### Documentation

- README: new "What's new in v0.8.0" section, new "Configuration → `.bash-mcp.toml`" subsection, "Out of scope" item struck through.
- AGENTS.md: new "## v0.8 (added 2026-09-13)" section + project_allowlist.py added to `src/` tree; test_project_allowlist.py added to `tests/` tree; test_executor.py / test_sessions.py counts bumped.

## [0.7.1] — 2026-09-13

### Added

- **`bash_mcp_audit_read(backup_index, max_entries?, tool_filter?)`** (`server.py`) — closes the v0.7 gzip one-way archival caveat. Transparently decompresses `audit.jsonl.N.gz` in addition to plaintext `audit.jsonl.N`. Returns `{backup_index, path, format ("plaintext"|"gzip"), total_in_file, returned, entries: [...]}`. Audited as `tool="bash_mcp_audit_read" outcome="READ"`. Wraps result in an object so the contract holds for any number of entries (the FastMCP unwrap-single-element-list bug from v0.7.0 taught us to always wrap).
- **Version bump** — `__version__` in `src/bash_mcp/__init__.py` bumped from `0.7.0` to `0.7.1`.

### Removed

- **`infra/install.sh.before-addendum`** — stray backup file from before v0.7. Untracked, never in git history. Safe to delete; not referenced anywhere.

### Tests

- **289 passing** (up from 274).
- New `tests/test_audit_read.py` — 12 unit tests covering plaintext + gzipped read-back, malformed-line skipping, max_entries cap, tool_filter, and corrupted .gz fail-soft.
- `tests/test_e2e.py` — 3 new live-server tests (`test_audit_read_e2e_current`, `_tool_filter`, `_nonexistent_returns_error`); `test_session_list_e2e` and `test_session_list_tool_returns_summaries` updated for the v0.7.1 `{sessions, count}` object contract.
- `infra/scripts/smoke-v07.py` — 8 new hot-test sections (3 audit_read + 5 status/version checks bumped from v0.7.0 → v0.7.1).

### Backwards compatibility

- All v0.7.0 callers see zero behavior change.
- 12 tools total (up from 11). `bash_mcp_audit_read` is purely additive.
- Audit log format unchanged. New audit entries (`tool="bash_mcp_audit_read" outcome="READ"`) are added when the new tool is called.

## [Unreleased] — agent-side system_prompt addendum

### Added
- **`infra/system-prompt-addendum.md`**: standalone markdown file containing the `bash-mcp`-vs-PowerShell-`wsl-d` rule that the mavis agent should follow in every session. Shipped as a first-class deployable alongside the skill, hook, and mcp.json entry.
- **`install.sh [5/5]`**: new step that reports on the addendum file (existence + sha256) and prints the canonical 3-step manual apply command. Idempotent (substring check). The apply itself is **not auto-invoked** from `install.sh` because the `mavis` CLI on both WSL (IDE launcher) and Windows (`mavis.cmd` references an unbundled `daemon/cli.js`) does not expose an `agent` subcommand. Apply is performed by the desktop `mavis agent update mavis` MCP tool, which `install.sh [5/5]` documents on stdout.

### Changed
- **`AGENTS.md`**: project tree now includes `system-prompt-addendum.md`; "three places that must stay in sync" extended to **four** (now includes the system_prompt addendum).
- **`README.md`**: `## Deploying` section adds a 5th bullet for the addendum; new `### Agent integration` sub-section documents why auto-apply is not supported and how to perform the manual apply.

### Backwards compatibility
- `install.sh [5/5]` is **purely additive**: it only reads `system-prompt-addendum.md`, prints info, and exits. It does not touch any deployed artifact, so reruns are safe even before the user has run the manual apply.

## [0.7.0] — 2026-09-12

### Added

- **`bash_mcp_classify(command)` denylist explainer** (`server.py`) — read-only MCP tool exposing `safety.classify()`. Lets the agent self-check a command before sending it. Returns `{class, matched_pattern, pattern_index, hint_with_dangerous_false, hint_with_dangerous_true, would_execute, would_execute_with_dangerous_true, audit_id}`. Does NOT execute; audited as `tool="bash_mcp_classify"`.
- **Session auto-TTL / idle eviction** (`sessions.py`, opt-in) — new env var `BASH_MCP_SESSION_IDLE_TIMEOUT_S` (default `0` = disabled). A singleton daemon thread (`_ensure_janitor()` / `_janitor_loop()` / `_evict_idle()`) is started lazily on first `create()` or `apply_state_update()` and evicts sessions whose `last_used_at` is older than the timeout. Sweep interval clamped to `[5, 300]` seconds. Each eviction is audited as `tool="bash_mcp_session_destroy" reason="idle_ttl_expired" freed_env_vars=N`. `bash_mcp_status.sessions` gains `{idle_timeout_s, janitor_enabled}`.
- **Audit log gzip-on-rotation** (`audit.py`, opt-in) — new env var `BASH_MCP_AUDIT_GZIP_THRESHOLD_BYTES` (default `0` = disabled). After `_rotate_if_needed`, `_gzip_pass()` walks `.1..BACKUP_COUNT` and gzips any backup whose size exceeds the threshold to `.jsonl.N.gz` (chunked copy, lazy `import gzip`). Plaintext deleted on success; kept on failure (fail-soft with stderr warning). `backup_paths()` prefers `.gz` over plaintext when both exist. Decompression is **not** implemented in v0.7 (one-way archival; use `zcat audit.jsonl.N.gz`). `bash_mcp_status.audit` gains `{gzip_threshold_bytes, gzip_enabled}`.
- **Version bump** — `__version__` in `src/bash_mcp/__init__.py` bumped from `0.6.0` to `0.7.0`.

### Tests

- **274 passing** (up from 220).
- New `tests/test_classify_tool.py` — 15 unit tests covering class routing, matched_pattern / pattern_index shape, hints, `would_execute` flags, side-effect guarantee (no execution), audit_id presence, and empty-string handling.
- New `tests/test_sessions_ttl.py` — 20 unit tests covering TTL disabled-by-default, eviction cutoff (inclusive of old, exclusive of recent), audit shape (`reason="idle_ttl_expired"`, `freed_env_vars`, `session_id`), janitor thread lifecycle, sweep interval clamping (`[5, 300]`), sweep warning on failure, status surface, and concurrent `_evict_idle` idempotence.
- New `tests/test_audit_gzip.py` — 14 unit tests reusing the `temp_audit_dir` fixture from `test_audit_rotation.py`. Covers disabled-by-default, threshold-zero, threshold-above/skip, .gz suffix replacement, gzip magic bytes (`1f 8b`), roundtrip via `gzip.open`, env-configurable threshold, oldest-first compression, fail-soft behavior (plaintext survives + stderr warning), and status surface.
- `tests/test_e2e.py` — 5 new live-server tests (`test_classify_e2e_safe_command`, `_dangerous_command`, `_reject_command`, `_does_not_execute`, `test_session_ttl_status_reflects_default_e2e`). Existing `test_status` and `test_tools_list` updated for v0.7 fields and 11-tool toolset.

### Backwards compatibility

- **All three features default to off** (env vars set to `0`). Existing callers see zero behavior change.
- `bash_run_command`, `bash_check_env`, `bash_list_binaries`, `bash_which`, `bash_mcp_status`, `bash_mcp_session_create/run/destroy/list`, `echo` — unchanged. The 6 stateless + 4 session tools from v0.6 are byte-identical.
- Audit log format unchanged. `audit.jsonl` continues to be plaintext JSONL; only opt-in `.gz` files are added for backups.
- Session contract unchanged. v0.6's "process-lifetime, explicit destroy" promise is the default.

### Compatibility notes for ops

- **`audit.jsonl.{N}.gz`** — if you set `BASH_MCP_AUDIT_GZIP_THRESHOLD_BYTES > 0`, plaintext `.N` files become `.N.gz` after rotation. Any external log shipper that expects plaintext must use `zcat` or be updated to handle gzip. Decompression on read is **not** implemented in v0.7.
- **`BASH_MCP_SESSION_IDLE_TIMEOUT_S`** — once set, sessions will be silently evicted on the next janitor sweep past their idle window. Set this only if your callers can tolerate silent session destruction; otherwise keep at `0` (v0.6 default).
- **Janitor thread** — one daemon thread (`bash-mcp-session-janitor`) is started lazily. Memory cost is negligible (~8 MB stack). The thread dies with the process.

### Deployment

```bash
cd /home/jbecerra/projects/bash-mcp
git pull            # or this commit, once pushed
systemctl --user restart bash-mcp.service   # picks up the new bash_mcp_classify tool

# Opt-in to v0.7 features (defaults stay off):
export BASH_MCP_SESSION_IDLE_TIMEOUT_S=300        # evict idle sessions after 5 min
export BASH_MCP_AUDIT_GZIP_THRESHOLD_BYTES=5242880  # gzip backups > 5 MB
# Then restart the service.
```

Verify:
```bash
curl http://127.0.0.1:54321/mcp/ ...   -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"bash_mcp_classify","arguments":{"command":"sudo apt update"}}}'
# -> { "class": "dangerous", "would_execute": false, "would_execute_with_dangerous_true": true, ... }
```

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
