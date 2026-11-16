"""Session state — cwd + env persistence across calls.

v0.6 was process-lifetime only; **v0.9 adds disk persistence**. Every
mutation of a session is mirrored to a SQLite store
(`bash_mcp.session_store`), so the sessions that were alive before a
restart are restored on boot. Set `BASH_MCP_SESSION_PERSIST=0` to opt out
and keep the pre-v0.9 process-lifetime behaviour.

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
from bash_mcp import project_allowlist, session_store


DEFAULT_MAX_ENV_VARS = 256

# v0.11: how many shared envs may exist at the same time. Shared envs are
# small, but they are not tied to a session lifetime, so they need a cap.
MAX_SHARED_ENVS = 64
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


class SharedEnvNotFoundError(RuntimeError):
    """Raised when a named shared env does not exist."""


@dataclass
class SessionState:
    """A single session's persisted state."""

    session_id: str
    name: str
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    # Name of the shared env this session subscribes to (v0.11). The binding
    # lives in the store, not in the `sessions` table.
    shared_env: str | None = None
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
            "shared_env": self.shared_env,
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

    # Drop the disk copies too — an evicted session must not come back
    # on the next restore.
    for sid, _freed in evicted:
        session_store.delete(sid)

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


def create(
    name: str | None = None,
    cwd: str | None = None,
    shared_env: str | None = None,
) -> SessionState:
    """Create a new session, register it, return the SessionState.

    Args:
        name: Optional human-readable label (≤ MAX_SESSION_NAME_LEN chars).
        cwd:  Optional starting working directory. Must be under an
              allowed root (same rules as `executor.run`).
        shared_env: Optional name of a shared env to subscribe to (v0.11).
                    The env must already exist.

    Raises:
        ValueError: name too long / wrong type.
        InvalidCwdError: cwd not allowed or doesn't exist.
        SharedEnvNotFoundError: the named shared env does not exist.
    """
    if name is not None:
        if not isinstance(name, str):
            raise ValueError(f"name must be str, got {type(name).__name__}")
        if len(name) > MAX_SESSION_NAME_LEN:
            raise ValueError(
                f"name exceeds {MAX_SESSION_NAME_LEN} chars (got {len(name)})"
            )

    if shared_env is not None:
        if not isinstance(shared_env, str) or not shared_env.strip():
            raise ValueError("shared_env must be a non-empty string")
        if session_store.load_shared_env(shared_env) is None:
            raise SharedEnvNotFoundError(f"shared env '{shared_env}' does not exist")

    resolved_cwd = _resolve_cwd(cwd)
    session_id = _new_session_id()
    final_name = name if name is not None else _new_default_name(session_id)

    state = SessionState(
        session_id=session_id,
        name=final_name,
        cwd=resolved_cwd,
        shared_env=shared_env,
    )

    with _LOCK:
        _SESSIONS[session_id] = state

    if shared_env is not None:
        session_store.bind_session_env(session_id, shared_env)
    _persist(state)
    _ensure_janitor()
    return state


def fork(
    session_id: str,
    name: str | None = None,
    cwd: str | None = None,
) -> SessionState:
    """Create an independent copy of a session (v0.11).

    The copy starts with the parent's `cwd` and local `env` as they are
    *right now*; the parent is not modified in any way. A shared-env
    binding is inherited by reference, so the child keeps seeing that env
    live.

    Args:
        session_id: The session to copy.
        name: Optional name for the copy. Defaults to `<parent>-fork`.
        cwd: Optional working directory for the copy. Defaults to the
             parent's cwd.

    Raises:
        ValueError: no such session / bad name.
        InvalidCwdError: cwd not allowed or doesn't exist.
    """
    parent = get(session_id)
    if parent is None:
        raise ValueError(f"no session with id '{session_id}'")

    if name is not None:
        if not isinstance(name, str):
            raise ValueError(f"name must be str, got {type(name).__name__}")
        if len(name) > MAX_SESSION_NAME_LEN:
            raise ValueError(
                f"name exceeds {MAX_SESSION_NAME_LEN} chars (got {len(name)})"
            )

    resolved_cwd = _resolve_cwd(cwd if cwd is not None else parent.cwd)
    # Truncate the PARENT name (not the whole "parent-fork") so the
    # suffix always survives when the parent name is already at the cap.
    if name is not None:
        final_name = name
    else:
        suffix = "-fork"
        head = parent.name[: max(1, MAX_SESSION_NAME_LEN - len(suffix))]
        final_name = f"{head}{suffix}"

    child_id = _new_session_id()
    child = SessionState(
        session_id=child_id,
        name=final_name,
        cwd=resolved_cwd,
        env=dict(parent.env),
        shared_env=parent.shared_env,
    )

    with _LOCK:
        _SESSIONS[child_id] = child

    if child.shared_env is not None:
        session_store.bind_session_env(child_id, child.shared_env)
    _persist(child)
    _ensure_janitor()
    return child


def bind_shared_env(session_id: str, env_name: str | None) -> None:
    """Subscribe a session to a shared env, or unsubscribe it (v0.11).

    `env_name=None` detaches the session from whatever it was bound to.
    A session subscribes to at most one shared env; binding again replaces
    the previous one.

    Raises:
        ValueError: no such session / bad name.
        SharedEnvNotFoundError: the named shared env does not exist.
    """
    state = get(session_id)
    if state is None:
        raise ValueError(f"no session with id '{session_id}'")

    if env_name is None:
        with _LOCK:
            state.shared_env = None
        session_store.unbind_session_env(session_id)
        return

    if not isinstance(env_name, str) or not env_name.strip():
        raise ValueError("env_name must be a non-empty string")
    if session_store.load_shared_env(env_name) is None:
        raise SharedEnvNotFoundError(f"shared env '{env_name}' does not exist")

    with _LOCK:
        state.shared_env = env_name
    session_store.bind_session_env(session_id, env_name)


def unbind_shared_env_name(env_name: str) -> int:
    """Clear the in-memory binding of every session subscribed to `env_name`.

    Called after the shared env is deleted from the store, so no session
    keeps pointing at an env that no longer exists.

    Returns the number of sessions cleared.
    """
    cleared = 0
    with _LOCK:
        for state in _SESSIONS.values():
            if state.shared_env == env_name:
                state.shared_env = None
                cleared += 1
    return cleared


def run_environment(
    session_id: str,
) -> tuple[str | None, dict[str, str], str | None] | None:
    """Return `(cwd, merged env, shared env name)` for a session.

    The merged env layers the shared env (the base, re-read on every call
    — that is what makes it live) under the session's own env, which wins
    when both define the same key.

    Returns None when the session does not exist.
    """
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return None
    with _LOCK:
        state = _SESSIONS.get(session_id)
        if state is None:
            return None
        cwd = state.cwd
        local_env = dict(state.env)
        shared_name = state.shared_env

    shared_env: dict[str, str] = {}
    if shared_name:
        shared_env = session_store.load_shared_env(shared_name) or {}

    merged = dict(shared_env)
    merged.update(local_env)
    return cwd, merged, shared_name


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
    if state is not None:
        session_store.delete(session_id)
    return len(state.env) if state else 0


def list_active() -> list[dict]:
    """Return summaries of all active sessions, most-recently-used first.

    Each summary carries `shared_env` (the name it subscribes to) and
    `shared_env_keys` (the keys of that env — never its values).
    """
    with _LOCK:
        items = sorted(
            _SESSIONS.values(),
            key=lambda s: s.last_used_at,
            reverse=True,
        )
        snapshot = [(s.to_summary(), s.shared_env) for s in items]

    # Read the shared envs outside the lock (I/O) and only once per name.
    keys_by_name: dict[str, list[str]] = {}
    for name in {n for _, n in snapshot if n}:
        env = session_store.load_shared_env(name) or {}
        keys_by_name[name] = sorted(env.keys())

    out: list[dict] = []
    for summary, shared_name in snapshot:
        summary["shared_env"] = shared_name
        summary["shared_env_keys"] = (
            keys_by_name.get(shared_name, []) if shared_name else []
        )
        out.append(summary)
    return out


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
        if state is not None:
            _persist(state)
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

    # Outside the lock — the disk copy is updated after the state is
    # settled, so a slow filesystem cannot block other sessions.
    _persist(state)

    # Outside the lock — janitor thread startup is idempotent.
    _ensure_janitor()


# --- v0.9: disk persistence ---


def _persist(state: SessionState) -> None:
    """Mirror a session to the disk store. Best-effort, never raises."""
    try:
        session_store.save(
            state.session_id,
            state.name,
            state.cwd,
            state.env,
            state.created_at,
            state.last_used_at,
        )
    except Exception:  # noqa: BLE001 — persistence must never break a session
        pass


def restore() -> int:
    """Reload the persisted sessions into the registry.

    Called once when the server starts. Returns the number of sessions
    restored. Rows that can no longer be used — a cwd that was deleted, a
    cwd that is no longer under an allowed root, a malformed session id —
    are dropped from the store instead of being restored.
    """
    if not session_store.enabled():
        return 0

    restored = 0
    for row in session_store.load_all():
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
            session_store.delete(session_id if isinstance(session_id, str) else "")
            continue

        try:
            cwd = _resolve_cwd(row.get("cwd") or "")
        except (InvalidCwdError, OSError, TypeError, ValueError):
            session_store.delete(session_id)
            continue

        env = dict(row.get("env") or {})
        if len(env) > MAX_ENV_VARS:
            env = dict(list(env.items())[:MAX_ENV_VARS])

        state = SessionState(
            session_id=session_id,
            name=row.get("name") or _new_default_name(session_id),
            cwd=cwd,
            env=env,
            created_at=float(row.get("created_at") or time.time()),
            last_used_at=float(row.get("last_used_at") or time.time()),
        )
        with _LOCK:
            _SESSIONS[session_id] = state
        restored += 1

    # Re-attach the shared-env bindings (v0.11). A binding whose shared env
    # no longer exists is an orphan: drop it instead of restoring a session
    # that points at nothing.
    existing_envs = {row["name"] for row in session_store.list_shared_envs()}
    for sid, env_name in session_store.load_bindings().items():
        with _LOCK:
            state = _SESSIONS.get(sid)
            if state is not None:
                state.shared_env = env_name
        if state is None or env_name not in existing_envs:
            session_store.unbind_session_env(sid)
            with _LOCK:
                if state is not None:
                    state.shared_env = None

    return restored


def persistence_status() -> dict:
    """Snapshot of the disk-persistence layer (surfaced by bash_mcp_status)."""
    return {
        "enabled": session_store.enabled(),
        "path": str(session_store.db_path()),
        "persisted_sessions": session_store.count(),
    }
