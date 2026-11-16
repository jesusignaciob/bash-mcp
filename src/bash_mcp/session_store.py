"""SQLite-backed persistence for stateful sessions (v0.9).

Until v0.8 a session lived only in the server process: a restart (a WSL
reboot, a redeploy, a crash) wiped every `bash_mcp_session_*` and the agent
had to rebuild its cwd/env by hand. This module is the disk half of the
session lifecycle — one row per session, so `sessions.restore()` can bring
the previous set back on boot.

Design notes:
  * **Stdlib only.** `sqlite3` is in the standard library; no new
    dependency is added to the package.
  * **One connection per operation.** FastMCP runs tools in a thread pool,
    so a shared connection would need locking; opening per operation keeps
    the store trivially thread-safe and the write volume is tiny.
  * **Fail-soft, always.** A missing directory, an unwritable path or a
    corrupt database degrades to "no persistence" — it never raises into
    the tool call. Sessions keep working in memory.
  * **Opt-out.** `BASH_MCP_SESSION_PERSIST=0` disables the store entirely
    (nothing is written and nothing is read), restoring the pre-v0.9
    process-lifetime behaviour.
  * **Override the location.** `BASH_MCP_SESSION_DB_PATH` wins over the
    default `$XDG_DATA_HOME/bash-mcp/sessions.db`.

Location: $XDG_DATA_HOME/bash-mcp/sessions.db (default ~/.local/share/bash-mcp/sessions.db)
"""

from __future__ import annotations

import json
import os
import time
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 2

# Values of BASH_MCP_SESSION_PERSIST that mean "off".
_FALSY = {"0", "false", "no", "off"}

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    cwd          TEXT NOT NULL,
    env_json     TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL,
    last_used_at REAL NOT NULL
)
"""

_CREATE_SHARED_ENVS = """
CREATE TABLE IF NOT EXISTS shared_envs (
    name       TEXT PRIMARY KEY,
    env_json   TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""

_CREATE_SESSION_BINDINGS = """
CREATE TABLE IF NOT EXISTS session_env_bindings (
    session_id TEXT PRIMARY KEY,
    env_name   TEXT NOT NULL
)
"""

_UPSERT = """
INSERT INTO sessions
    (session_id, name, cwd, env_json, created_at, last_used_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(session_id) DO UPDATE SET
    name         = excluded.name,
    cwd          = excluded.cwd,
    env_json     = excluded.env_json,
    last_used_at = excluded.last_used_at
"""

_SELECT_ALL = """
SELECT session_id, name, cwd, env_json, created_at, last_used_at
FROM sessions
"""


def enabled() -> bool:
    """True when the store should be read/written.

    Controlled by `BASH_MCP_SESSION_PERSIST` (default: on).
    """
    raw = os.environ.get("BASH_MCP_SESSION_PERSIST", "1").strip().lower()
    return raw not in _FALSY


def db_path() -> Path:
    """Resolve the database path.

    `BASH_MCP_SESSION_DB_PATH` wins; otherwise `$XDG_DATA_HOME/bash-mcp/
    sessions.db` (default `~/.local/share/bash-mcp/sessions.db`).
    """
    override = os.environ.get("BASH_MCP_SESSION_DB_PATH")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "bash-mcp" / "sessions.db"


def _warn(message: str) -> None:
    print(f"[bash-mcp sessions] WARN: {message}", file=sys.stderr)


@contextmanager
def _db():
    """Open a connection, create the schema, and always close it.

    The caller is responsible for handling `sqlite3.Error` / `OSError`.
    """
    conn = None
    try:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0)
        with conn:
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_SHARED_ENVS)
            conn.execute(_CREATE_SESSION_BINDINGS)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            yield conn
    finally:
        if conn is not None:
            conn.close()


def save(
    session_id: str,
    name: str,
    cwd: str,
    env: dict[str, str],
    created_at: float,
    last_used_at: float,
) -> None:
    """Insert or update the row for `session_id`. Never raises."""
    if not enabled():
        return
    try:
        payload = json.dumps(env, ensure_ascii=False, sort_keys=True)
        with _db() as conn:
            conn.execute(
                _UPSERT,
                (
                    session_id,
                    name,
                    cwd,
                    payload,
                    float(created_at),
                    float(last_used_at),
                ),
            )
    except (sqlite3.Error, OSError, TypeError, ValueError) as e:
        _warn(f"could not persist session {session_id}: {e}")


def delete(session_id: str) -> None:
    """Remove the row for `session_id`. Never raises."""
    if not enabled():
        return
    try:
        with _db() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            # The store owns both tables: a deleted session leaves no binding.
            conn.execute(
                "DELETE FROM session_env_bindings WHERE session_id = ?", (session_id,)
            )
    except (sqlite3.Error, OSError) as e:
        _warn(f"could not delete session {session_id}: {e}")


def load_all() -> list[dict]:
    """Return every persisted session as a dict. Never raises.

    Each dict has the keys `session_id`, `name`, `cwd`, `env`, `created_at`
    and `last_used_at`. A row with an unparseable `env_json` is returned
    with an empty env rather than being dropped.
    """
    if not enabled():
        return []
    try:
        with _db() as conn:
            rows = conn.execute(_SELECT_ALL).fetchall()
    except (sqlite3.Error, OSError) as e:
        _warn(f"could not read the session store: {e}")
        return []

    out: list[dict] = []
    for session_id, name, cwd, env_json, created_at, last_used_at in rows:
        env: dict[str, str] = {}
        try:
            parsed = json.loads(env_json) if env_json else {}
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            env = {str(k): str(v) for k, v in parsed.items()}
        out.append(
            {
                "session_id": session_id,
                "name": name,
                "cwd": cwd,
                "env": env,
                "created_at": created_at,
                "last_used_at": last_used_at,
            }
        )
    return out


def count() -> int:
    """Number of persisted sessions. Never raises."""
    if not enabled():
        return 0
    try:
        with _db() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
    except (sqlite3.Error, OSError) as e:
        _warn(f"could not count persisted sessions: {e}")
        return 0


# --- shared envs (v0.11) ---


def save_shared_env(name: str, env: dict[str, str]) -> None:
    """Create or replace a named shared env. Never raises.

    The whole dict is replaced; merging happens one layer up (the
    `bash_mcp_env_share` tool), so this stays a predictable store API.
    """
    if not enabled():
        return
    now = time.time()
    try:
        payload = json.dumps(env, ensure_ascii=False, sort_keys=True)
        with _db() as conn:
            conn.execute(
                "INSERT INTO shared_envs (name, env_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "env_json = excluded.env_json, updated_at = excluded.updated_at",
                (name, payload, now, now),
            )
    except (sqlite3.Error, OSError, TypeError, ValueError) as e:
        _warn(f"could not persist shared env {name!r}: {e}")


def load_shared_env(name: str) -> dict[str, str] | None:
    """Return the dict of a shared env, or None when it does not exist.

    This is the only place where the *values* of a shared env are read;
    they are never returned to a caller.
    """
    if not enabled():
        return None
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT env_json FROM shared_envs WHERE name = ?", (name,)
            ).fetchone()
    except (sqlite3.Error, OSError) as e:
        _warn(f"could not read shared env {name!r}: {e}")
        return None
    if row is None:
        return None
    try:
        parsed = json.loads(row[0]) if row[0] else {}
    except (TypeError, ValueError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return {str(k): str(v) for k, v in parsed.items()}


def list_shared_envs() -> list[dict]:
    """Return every shared env WITHOUT its values.

    Each entry has `name`, `env_count`, `env_keys`, `created_at` and
    `updated_at`. Values are deliberately absent: the listing is safe to
    hand back to a caller that may be logging it.
    """
    if not enabled():
        return []
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT name, env_json, created_at, updated_at "
                "FROM shared_envs ORDER BY name"
            ).fetchall()
    except (sqlite3.Error, OSError) as e:
        _warn(f"could not list shared envs: {e}")
        return []

    out: list[dict] = []
    for name, env_json, created_at, updated_at in rows:
        keys: list[str] = []
        try:
            parsed = json.loads(env_json) if env_json else {}
            if isinstance(parsed, dict):
                keys = sorted(str(k) for k in parsed)
        except (TypeError, ValueError):
            keys = []
        out.append(
            {
                "name": name,
                "env_count": len(keys),
                "env_keys": keys,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
    return out


def delete_shared_env(name: str) -> int:
    """Delete a shared env and unbind every session that used it.

    Returns the number of sessions unbound (0 when the env did not exist).
    """
    if not enabled():
        return 0
    try:
        with _db() as conn:
            cursor = conn.execute(
                "DELETE FROM session_env_bindings WHERE env_name = ?", (name,)
            )
            unbound = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            conn.execute("DELETE FROM shared_envs WHERE name = ?", (name,))
        return unbound
    except (sqlite3.Error, OSError) as e:
        _warn(f"could not delete shared env {name!r}: {e}")
        return 0


def bind_session_env(session_id: str, env_name: str) -> None:
    """Bind a session to a shared env (one env per session). Never raises."""
    if not enabled():
        return
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO session_env_bindings (session_id, env_name) "
                "VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET env_name = excluded.env_name",
                (session_id, env_name),
            )
    except (sqlite3.Error, OSError) as e:
        _warn(f"could not bind session {session_id} to shared env {env_name!r}: {e}")


def unbind_session_env(session_id: str) -> None:
    """Remove the shared-env binding of a session. Never raises."""
    if not enabled():
        return
    try:
        with _db() as conn:
            conn.execute(
                "DELETE FROM session_env_bindings WHERE session_id = ?", (session_id,)
            )
    except (sqlite3.Error, OSError) as e:
        _warn(f"could not unbind session {session_id}: {e}")


def load_bindings() -> dict[str, str]:
    """Return `{session_id: env_name}` for every binding on disk."""
    if not enabled():
        return {}
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT session_id, env_name FROM session_env_bindings"
            ).fetchall()
    except (sqlite3.Error, OSError) as e:
        _warn(f"could not read the session bindings: {e}")
        return {}
    return {str(sid): str(name) for sid, name in rows}
