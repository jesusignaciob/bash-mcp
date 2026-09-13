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
├── server.py       # FastMCP server + 12 tools (@mcp.tool) — 6 stateless + 4 session + 1 classify + 1 audit-read (v0.6/v0.7/v0.7.1)
├── executor.py     # subprocess.run wrapper, timeout, truncation, cwd allowlist, concurrency slot
├── safety.py       # classify(command) -> Class.{SAFE,DANGEROUS,REJECT}
├── audit.py        # JSONL append-only logger + size-based rotation (v0.3) + opt-in gzip-on-rotation (v0.7) + read-back (v0.7.1)
├── discovery.py    # which / list_binaries (thread-pool based)
├── concurrency.py  # threading.BoundedSemaphore wrapper (v0.3)
└── sessions.py     # stateful session registry (cwd + env); process-lifetime (v0.6) + opt-in auto-TTL janitor (v0.7)
tests/
├── test_safety.py            # 115 parametrized denylist cases
├── test_executor.py          # 27 cases: subprocess + cwd allowlist + Windows→WSL
├── test_errors.py            # 9 cases: error envelope shape + hint text
├── test_audit_rotation.py    # 6 cases: rotation + concurrency (v0.3)
├── test_concurrency.py       # 5 cases: semaphore behavior (v0.3)
├── test_classify_tool.py     # 15 cases: explainer tool (v0.7)
├── test_audit_read.py        # 12 cases: read-back (v0.7.1)
├── test_sessions_ttl.py      # 20 cases: auto-TTL / janitor (v0.7)
├── test_audit_gzip.py        # 14 cases: gzip-on-rotation (v0.7)
├── test_sessions.py          # 51 cases: registry + parsing + 4 tools (v0.6)
└── test_e2e.py               # 21 cases: live server round-trip (incl. 5 session e2e + 5 classify e2e)
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
├── system-prompt-addendum.md         # canonical rule text for agent system_prompt (manual apply via desktop mavis tool; see README "Agent integration")
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
- **Adding an enforcement layer**: update `C:\Users\jesus\.mavis\skills\bash-mcp\SKILL.md` (source: `infra/skills/bash-mcp/SKILL.md`) AND `C:\Users\jesus\.minimax\agents\mavis\hooks\bash-mcp-redirect.md` (source: `infra/hooks/bash-mcp-redirect.{md,js}`) AND the `description` field in `mcp.json` AND `infra/system-prompt-addendum.md` (deployed manually via the desktop mavis tool) — **four places** that share the same escape hatch name (`BASH_MCP_SKIP=1`) and cross-reference each other. Keep them in sync.

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


## v0.6 sessions (added 2026-09-12)

Four new tools exposed under `bash-mcp_session_*`:

- `bash_mcp_session_create(name?, cwd?) -> {session_id, ...}`
- `bash_mcp_session_run(session_id, command, timeout_ms?, dangerous?) -> result`
- `bash_mcp_session_destroy(session_id) -> {ok, ...}`
- `bash_mcp_session_list() -> [...]`

Key design points (see `plan.md` for the full discussion):

- **Process-lifetime only** — `dict[str, SessionState]` at module level, guarded by `threading.Lock`. A server restart wipes sessions. There is no auto-TTL.
- **Per-session env cap** — default 256 vars, configurable via `BASH_MCP_SESSION_MAX_ENV_VARS`. Silently drops new vars past the cap; overwriting existing keys is allowed.
- **Best-effort parsing** — top-level `cd PATH`, `export VAR=VALUE`, `unset VAR` (chained with `;` / `&&` / `&`) are extracted by regex. Anything more complex (heredocs, `$(...)`, function bodies, multi-line scripts) is intentionally out of scope.
- **Quoted export values are stored literally** — no shell expansion. `export X="$HOME"` records `$HOME`; the subprocess in the same call still sees the expanded value via `bash -lc`.
- **Reuses executor.run** — concurrency.slot() and the cwd allowlist apply unchanged. HARD denylist still rejects unconditionally; SOFT requires `dangerous=true`.
- **Audit integration** — `bash_mcp_session_run` writes the same audit entry shape as `bash_run_command` plus `args.session_id`. Create/destroy write small entries via `audit.log(...)`.

When extending sessions:
1. Add new logic to `src/bash_mcp/sessions.py`; do not mutate the registry outside this module.
2. New server tools must call `snapshot_for_run` to clone state under the lock, release the lock, then run `exec_run` (which itself wraps `concurrency.slot()`). Re-acquire the lock after to apply state updates.
3. Re-check session existence after `exec_run` — destroy-during-run races must no-op, not crash.
4. Update `bash_mcp_status.tools` list when adding a new tool.
5. Tests go in `tests/test_sessions.py` (unit, no live server) and `tests/test_e2e.py` (live server).


## v0.7 (added 2026-09-12)

Three **opt-in** additive features. All three default to off via env vars; existing callers see zero behavior change.

### bash_mcp_classify

Pure wrapper around `safety.classify()`. Read-only. Lets the agent self-check a command before sending it. Returns `{class, matched_pattern, pattern_index, hint_with_dangerous_false, hint_with_dangerous_true, would_execute, would_execute_with_dangerous_true, audit_id}`. Audited as `tool="bash_mcp_classify"` (no subprocess).

### Session auto-TTL

- New env var: `BASH_MCP_SESSION_IDLE_TIMEOUT_S` (default 0 = disabled).
- Singleton daemon thread started lazily on first `create()` or `apply_state_update()`.
- Sweep interval: `max(5, min(300, timeout // 4))` seconds.
- Each eviction: `tool="bash_mcp_session_destroy" reason="idle_ttl_expired" freed_env_vars=N`.
- Lock: `_JANITOR_LOCK` (separate from `_LOCK`) for thread startup; evictions go through the existing `_LOCK`.
- `bash_mcp_status.sessions` gains `{idle_timeout_s, janitor_enabled}`.

### Audit log gzip-on-rotation

- New env var: `BASH_MCP_AUDIT_GZIP_THRESHOLD_BYTES` (default 0 = disabled).
- After `_rotate_if_needed`, walks `.1..BACKUP_COUNT` and gzips any backup exceeding the threshold. Plaintext deleted on success; kept on failure (fail-soft with stderr warning).
- `import gzip` is lazy inside `_gzip_pass` so non-users don't pay the import cost.
- `backup_paths()` prefers `.gz` over plaintext when both exist (gz supersedes).
- Decompression is **not** implemented in v0.7 — one-way archival; use `zcat audit.jsonl.N.gz`.
- `bash_mcp_status.audit` gains `{gzip_threshold_bytes, gzip_enabled}`.

### v0.7 test layout

- `test_classify_tool.py` (15) — unit, no live server.
- `test_sessions_ttl.py` (20) — unit, calls `_evict_idle()` directly for determinism.
- `test_audit_gzip.py` (14) — unit, uses the `temp_audit_dir` fixture pattern.
- `test_e2e.py` (+5) — live-server round-trip for the new tool + status surface.

### When extending opt-in features

Pattern (all three follow it):
1. New module-level constant: `<NAME>: int = int(os.environ.get("<ENV_VAR>", str(DEFAULT)))`.
2. Default `DEFAULT = 0` = disabled (preserves prior contract).
3. Behavior guarded by `if <NAME> > 0:`.
4. `bash_mcp_status` surfaces the constant + a `_enabled` flag.
5. Document the env var + semantics in SKILL.md and CHANGELOG.


## v0.7.1 (added 2026-09-13)

Patch release. Adds `bash_mcp_audit_read` (12th tool) and cleans up a stray file.

### bash_mcp_audit_read

Read audit log entries from a backup. Closes the v0.7 gzip one-way archival caveat by transparently decompressing `.jsonl.N.gz`. Returns a stable `{backup_index, path, format ("plaintext"|"gzip"), entries, returned}` object so the contract holds whether there are 0, 1, or many entries (the FastMCP unwrap-single-element-list bug from v0.7.0 taught us to always wrap).

Audited as `tool="bash_mcp_audit_read" outcome="READ"`. Reads are themselves logged.

### Cleanup

Deleted `infra/install.sh.before-addendum` (untracked, never in git history). Safe to delete; not referenced anywhere.

### v0.7.1 test layout

- `test_audit_read.py` (12) — unit, no live server.
- `tests/test_e2e.py` (+3) — live-server round-trip for the new tool.
- `infra/scripts/smoke-v07.py` (+8) — hot-test additions.

## v0.8 (added 2026-09-13)

Per-project cwd allowlists via ``.bash-mcp.toml``. Each project can drop a
TOML file at the repo root (or any ancestor) and bash-mcp picks it up for
calls whose cwd lives under that directory. Zero behavior change when no
file is present.

### v0.8 design

- **New module: `src/bash_mcp/project_allowlist.py`.** Public surface:
  `load(start_dir) -> ProjectAllowlist | None`,
  `effective_allowed_roots(start_dir, global_roots) -> tuple[str, ...] | None`,
  `clear_cache()` (test helper), `ProjectAllowlist` dataclass.
- **Walk-up semantics.** Starting from the call's resolved cwd, walk up
  parent dirs until the first ``.bash-mcp.toml`` is found. Stop at
  ``$HOME`` or filesystem root. Capped at 32 hops as a safety belt.
- **Two modes.**

  - ``extend`` (default) — concatenate global + project, de-duplicated.
  - ``replace`` — return ONLY the project's roots. Empty list is a
    deliberate lockout (returns ``()``, not ``None``).

- **Fail-soft.** Malformed TOML, unknown mode, non-list
  ``allowed_roots`` → stderr warning + fall back to global. Never crashes.
- **TOML library.** ``tomli>=2.0`` on Python < 3.11; ``tomllib`` stdlib
  on 3.11+. Added as a direct dependency in ``pyproject.toml``.
- **Cache.** Module-level ``dict[(start_dir_str, mtime_ns), ProjectAllowlist | None]``
  for the lifetime of the bash-mcp process. Editing the TOML
  invalidates the cached entry automatically (mtime changes).
  Negative results (no file found) are cached too with ``mtime_ns == -1``.
- **executor + sessions integration.** `_is_under_allowed_root` gets an
  optional ``effective_roots`` parameter (defaults to ``ALLOWED_CWD_ROOTS``).
  Both `executor.run()` and `sessions._resolve_cwd()` compute
  ``effective_allowed_roots(cwd, ALLOWED_CWD_ROOTS)`` before the check.
  Zero behavior change when no ``.bash-mcp.toml`` is found (the helper
  returns ``None`` and the global roots are used unchanged).
- **Server hint.** `_hint_for_invalid_cwd` mentions the project allowlist
  source + roots when one is in effect (so the user understands why
  ``INVALID_CWD`` is reported even though the path is under a global
  root).
- **No new MCP tool.** v0.8 is config-only; the tool count stays at 12.

### v0.8 test layout

- `tests/test_project_allowlist.py` (+11) — walk-up, cache invalidation,
  extend/replace/empty-lockout, fail-soft on malformed TOML / unknown
  mode / non-list `allowed_roots`.
- `tests/test_executor.py` (+3) — extend allows, replace drops global,
  no-TOML falls back to global.
- `tests/test_sessions.py` (+2) — session_create accepts project root;
  session_create rejected by replace-mode lockout.
- `infra/scripts/smoke-v07.py` (+1 section, ~3 cases) — live-server
  per-project allowlist round-trip.

### v0.8 deployment notes

- No install.sh change. The feature is opt-in: projects add a
  ``.bash-mcp.toml`` themselves; nothing is auto-created.
- No schema-validation library. We use stdlib ``tomllib`` / ``tomli``
  directly.
- The cache is process-lifetime only. Restart the bash-mcp service
  (`systemctl --user restart bash-mcp.service`) to drop stale entries
  after adding/removing TOMLs in dirs that were never queried before.
