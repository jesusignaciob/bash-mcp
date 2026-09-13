"""Session state — cwd + env persistence across calls.

Process-lifetime only. No disk persistence in v0.6.

Design notes:
  * `SessionState` is a dataclass: {session_id, name, cwd, env,
    created_at, last_used_at}.
  * `_SESSIONS: dict[str, SessionState]` is module-level, guarded by
    `_LOCK`.
  * `cd` and `export`/`unset` updates run after a successful execution
    (we know the command didn't fail) and under `_LOCK`.
  * Reads (`session_run` callers) clone `cwd` + `env` under the lock,
    then release before invoking `executor.run` — keeps the lock window
    tiny.
  * Memory safety: per-session env dict capped at `MAX_ENV_VARS` (default
    256; configurable via `BASH_MCP_SESSION_MAX_ENV_VARS`).
  * Concurrency: each `session_run` still acquires
    `bash_mcp.concurrency.slot()` — sessions do NOT escape the global
    limit (default 8).

Parsing limitations (documented in tool descriptions):
  * Only top-level `cd` and `export`/`unset` statements on simple
    command chains are tracked. Shell scripts, functions, `if`/`while`
    bodies, `$(...)`, backticks, heredocs, multi-line `\\` continuations
    are out of scope.
  * Quoted `export` values store the LITERAL text — no shell expansion.
    `export X="$HOME"` stores `$HOME` (the subprocess sees the real
    value because `bash -lc` expanded it, but session.env does not).
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

from bash_mcp.executor import (
    ALLOWED_CWD_ROOTS,
    InvalidCwdError,
    _convert_windows_path,
    _is_under_allowed_root,
)
from bash_mcp import project_allowlist


DEFAULT_MAX_ENV_VARS = 256
MAX_SESSION_NAME_LEN = 64
SESSION_ID_PREFIX = "s_"

# Conservative session_id format — UUID4 hex prefixed with `s_` (so 34 chars).
SESSION_ID_RE = re.compile(r"^s_[0-9A-F]{32}$")

# `cd` at start of command OR after ; / & / &&. Captures the quote char
# and the path. Bareword paths only; no shell-expansion.
_CD_RE = re.compile(
    r"""(?:^|[;&]\s*)
    cd\s+
    (['"]?)                # 1: optional quote
    (                     # 2: path
      (?:\\.|[^'"\s;&\\])*
    )
    \1                    # matching closing quote (or empty)
    """,
    re.VERBOSE,
)

# `export NAME=VALUE` with single, double, or bareword value.
#   1: name, 2: single-quoted value, 3: double-quoted value, 4: bareword value
_EXPORT_RE = re.compile(
    r"""(?:^|[;&]\s*)
    export\s+
    ([A-Za-z_][A-Za-z0-9_]*)
    =
    (?:
      '([^']*)'           # 2: single-quoted (no escapes, by design)
      |
      "([^"]*)"           # 3: double-quoted (no escapes, by design)
      |
      (                   # 4: bareword until ; & " ' or whitespace
        (?:\\.|[^\s;&'"\\])*
      )
    )
    """,
    re.VERBOSE,
)

# `unset NAME` at top-level.
_UNSET_RE = re.compile(
    r"""(?:^|[;&]\s*)
    unset\s+
    ([A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)


@dataclass
class SessionState:
    """A single session's persisted state."""

    session_id: str
    name: str
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    def to_summary(self) -> dict:
        """Compact representation for list_active()."""
        return {
            "session_id": self.session_id,
            "name": self.name,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "env_count": len(self.env),
            "env_keys": sorted(self.env.keys()),
        }


# Module-level store + lock.
_SESSIONS: dict[str, SessionState] = {}
_LOCK = threading.Lock()

# Janitor thread (v0.7). Singleton daemon started lazily on the
# first create() / apply_state_update() when IDLE_TIMEOUT_S > 0.
_JANITOR_STARTED = False
_JANITOR_LOCK = threading.Lock()

# Configurable cap (read once at import; tests may mutate directly).
MAX_ENV_VARS: int = int(
    os.environ.get("BASH_MCP_SESSION_MAX_ENV_VARS", str(DEFAULT_MAX_ENV_VARS))
)

# v0.7: session auto-TTL / idle eviction. When > 0, a daemon
# janitor thread periodically evicts sessions whose last_used_at is
# v0.6 "process-lifetime, explicit destroy" contract).
DEFAULT_IDLE_TIMEOUT_S: int = 0
IDLE_TIMEOUT_S: int = int(
    os.environ.get("BASH_MCP_SESSION_IDLE_TIMEOUT_S", str(DEFAULT_IDLE_TIMEOUT_S))
)


def _new_session_id() -> str:
    """UUID4 hex prefixed with `s_` (e.g. `s_4F3A...`)."""
    return SESSION_ID_PREFIX + uuid.uuid4().hex.upper()


def _new_default_name(session_id: str) -> str:
    return f"session-{session_id[len(SESSION_ID_PREFIX):][:8]}"


def _janitor_sweep_interval_s() -> int:
    """Compute the janitor sweep interval.

    Sleeps IDLE_TIMEOUT_S // 4 between sweeps, clamped to [5, 300] seconds
    so we never busy-loop (very small timeout) or sleep too long (very
    large timeout).
    """
    base = IDLE_TIMEOUT_S // 4
    if base <= 0:
        base = 5
    return max(5, min(300, base))


def _ensure_janitor() -> None:
    """Start the eviction janitor if idle TTL is enabled.

    Idempotent — safe to call from every create() and apply_state_update().
    The thread is daemon so it dies with the process.
    """
    global _JANITOR_STARTED
    if IDLE_TIMEOUT_S <= 0 or _JANITOR_STARTED:
        return
    with _JANITOR_LOCK:
        if _JANITOR_STARTED:
            return
        _JANITOR_STARTED = True
        sweep_s = _janitor_sweep_interval_s()
        t = threading.Thread(
            target=_janitor_loop,
            args=(sweep_s,),
            name="bash-mcp-session-janitor",
            daemon=True,
        )
        t.start()


def _janitor_loop(sweep_s: int) -> None:
    """Forever-running daemon body. Sleeps, then sweeps, then repeats."""
    import sys  # local import to keep module top-level clean
    while True:
        # Sleep first so a server that starts + immediately shuts down
        # doesn't have to do any sweeps.
        time.sleep(sweep_s)
        try:
            _evict_idle()
        except Exception as e:  # noqa: BLE001 — best-effort janitor
            print(
                f"[bash-mcp sessions] WARN: janitor sweep failed: {e}",
                file=sys.stderr,
            )


def _evict_idle() -> int:
    """Evict sessions whose last_used_at is older than IDLE_TIMEOUT_S.

    Returns the number of sessions evicted. Safe to call directly from
    tests (no daemon thread needed — tests reset _JANITOR_STARTED).
    Audits each eviction under tool="bash_mcp_session_destroy".
    """
    if IDLE_TIMEOUT_S <= 0:
        return 0
    cutoff = time.time() - IDLE_TIMEOUT_S
    evicted: list[tuple[str, int]] = []  # (session_id, env_count)

    with _LOCK:
        stale = [
            sid for sid, s in _SESSIONS.items()
            if s.last_used_at < cutoff
        ]
        for sid in stale:
            state = _SESSIONS.pop(sid, None)
            if state is not None:
                evicted.append((sid, len(state.env)))

    # Audit each eviction OUTSIDE the lock. Audit is best-effort: a
    # broken audit log does NOT block the eviction itself.
    for sid, freed in evicted:
        try:
            from bash_mcp import audit  # local import: avoid cycles at import time
            audit.log({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "audit_id": f"ttl-{uuid.uuid4().hex[:8]}",
                "tool": "bash_mcp_session_destroy",
                "args": {"session_id": sid},
                "outcome": "DESTROYED",
                "reason": "idle_ttl_expired",
                "freed_env_vars": freed,
            })
        except Exception:
            pass  # audit is best-effort

    return len(evicted)


def _reset_janitor() -> None:
    """Test helper: mark the janitor as not-started so tests can re-init."""
    global _JANITOR_STARTED
    _JANITOR_STARTED = False


def _resolve_cwd(cwd: str | None) -> str:
    """Resolve + validate a cwd, raising InvalidCwdError on failure.

    Mirrors the resolution in `executor.run` (Windows→WSL conversion,
    ~ expansion, absolute-path resolution, allowlist, existence, is_dir).
    """
    if cwd is None:
        effective = os.path.expanduser("~")
    else:
        effective = _convert_windows_path(cwd)
        effective = os.path.expanduser(effective)
        if not os.path.isabs(effective):
            effective = os.path.abspath(
                os.path.join(os.path.expanduser("~"), effective)
            )

    # v0.8: per-project allowlist (same semantics as executor.run).
    effective_roots = project_allowlist.effective_allowed_roots(
        effective, ALLOWED_CWD_ROOTS
    )
    if not _is_under_allowed_root(effective, effective_roots):
        if effective_roots is None:
            allowed_msg = ", ".join(ALLOWED_CWD_ROOTS)
        else:
            allowed_msg = ", ".join(effective_roots)
        raise InvalidCwdError(
            f"cwd not under an allowed root: {effective}; "
            f"allowed: {allowed_msg}"
        )

    if not os.path.exists(effective):
        raise InvalidCwdError(f"cwd does not exist: {effective}")
    if not os.path.isdir(effective):
        raise InvalidCwdError(f"cwd is not a directory: {effective}")

    return effective


# --- Parsing helpers (pure functions, no state) ---


def _parse_cd_targets(command: str) -> list[str]:
    """Return ordered list of `cd` target paths found in command."""
    return [m.group(2) for m in _CD_RE.finditer(command)]


def _parse_exports(command: str) -> list[tuple[str, str]]:
    """Return list of (name, value) tuples parsed from `export` statements."""
    out: list[tuple[str, str]] = []
    for m in _EXPORT_RE.finditer(command):
        name = m.group(1)
        if m.group(2) is not None:
            value = m.group(2)
        elif m.group(3) is not None:
            value = m.group(3)
        else:
            value = m.group(4) or ""
        out.append((name, value))
    return out


def _parse_unsets(command: str) -> list[str]:
    """Return list of variable names from `unset` statements."""
    return [m.group(1) for m in _UNSET_RE.finditer(command)]


# --- Public API ---


def create(name: str | None = None, cwd: str | None = None) -> SessionState:
    """Create a new session, register it, return the SessionState.

    Args:
        name: Optional human-readable label (≤ MAX_SESSION_NAME_LEN chars).
        cwd:  Optional starting working directory. Must be under an
              allowed root (same rules as `executor.run`).

    Raises:
        ValueError: name too long / wrong type.
        InvalidCwdError: cwd not allowed or doesn't exist.
    """
    if name is not None:
        if not isinstance(name, str):
            raise ValueError(f"name must be str, got {type(name).__name__}")
        if len(name) > MAX_SESSION_NAME_LEN:
            raise ValueError(
                f"name exceeds {MAX_SESSION_NAME_LEN} chars (got {len(name)})"
            )

    resolved_cwd = _resolve_cwd(cwd)
    session_id = _new_session_id()
    final_name = name if name is not None else _new_default_name(session_id)

    state = SessionState(
        session_id=session_id,
        name=final_name,
        cwd=resolved_cwd,
    )

    with _LOCK:
        _SESSIONS[session_id] = state

    _ensure_janitor()
    return state


def get(session_id: str) -> SessionState | None:
    """Return the SessionState for `session_id`, or None."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return None
    with _LOCK:
        return _SESSIONS.get(session_id)


def destroy(session_id: str) -> int:
    """Destroy a session. Returns number of env vars freed (0 if missing)."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return 0
    with _LOCK:
        state = _SESSIONS.pop(session_id, None)
    return len(state.env) if state else 0


def list_active() -> list[dict]:
    """Return summaries of all active sessions, most-recently-used first."""
    with _LOCK:
        items = sorted(
            _SESSIONS.values(),
            key=lambda s: s.last_used_at,
            reverse=True,
        )
        return [s.to_summary() for s in items]


def active_count() -> int:
    """Cheap count (no allocation). Used by bash_mcp_status."""
    with _LOCK:
        return len(_SESSIONS)


def snapshot_for_run(session_id: str) -> tuple[str | None, dict[str, str] | None]:
    """Clone (cwd, env) under the lock for use by a session_run call.

    Returns (cwd, env_copy) on success; (None, None) if session missing.
    The env is a copy so the caller can mutate freely.
    """
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return None, None
    with _LOCK:
        state = _SESSIONS.get(session_id)
        if state is None:
            return None, None
        return state.cwd, dict(state.env)


def apply_state_update(
    session_id: str, command: str, exit_code: int
) -> None:
    """Parse command for cd / export / unset and update session state.

    Only runs when exit_code == 0. Re-checks session existence under the
    lock to handle destroy-during-run races (no-op if session is gone).

    Raises nothing — invalid cd targets are silently skipped (the
    subprocess already saw the cd fail and the exit_code reflects it;
    if the cd succeeded in the subprocess but the resolved target
    fails our post-run allowlist check, we drop the update silently
    and the next session_run will use the unchanged cwd).
    """
    if exit_code != 0:
        # Even on failure we update last_used_at if the session still exists,
        # so list_active stays accurate.
        with _LOCK:
            state = _SESSIONS.get(session_id)
            if state is not None:
                state.last_used_at = time.time()
        # Outside the lock — janitor thread startup is idempotent.
        _ensure_janitor()
        return

    with _LOCK:
        state = _SESSIONS.get(session_id)
        if state is None:
            return

        # 1) cd — last one wins; only persist if final target is valid.
        cd_targets = _parse_cd_targets(command)
        if cd_targets:
            try:
                new_cwd = _resolve_cwd(cd_targets[-1])
            except InvalidCwdError:
                pass
            else:
                state.cwd = new_cwd

        # 2) export — accumulate; reject overflow.
        for name, value in _parse_exports(command):
            if (
                len(state.env) >= MAX_ENV_VARS
                and name not in state.env
            ):
                continue
            state.env[name] = value

        # 3) unset — drop from session.env if present.
        for name in _parse_unsets(command):
            state.env.pop(name, None)

        state.last_used_at = time.time()

    # Outside the lock — janitor thread startup is idempotent.
    _ensure_janitor()

    # Outside the lock — janitor thread startup is idempotent.
    _ensure_janitor()
