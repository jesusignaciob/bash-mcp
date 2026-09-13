"""Unit tests for stateful sessions (v0.6).

Covers `bash_mcp.sessions` (dataclass, registry, parsing, apply) and the
4 new server tools (`bash_mcp_session_create / run / destroy / list`).

Tests are isolated via `_reset_sessions()` which clears the module-level
dict + restores the default MAX_ENV_VARS. They do NOT need a live server.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

from bash_mcp import concurrency, sessions
from bash_mcp.executor import InvalidCwdError


# --- helpers ---


def _reset_sessions(max_env_vars: int | None = None) -> None:
    """Reset the module-level sessions dict + MAX_ENV_VARS.

    Each test starts with a clean registry so order doesn't matter.
    """
    with sessions._LOCK:
        sessions._SESSIONS.clear()
    if max_env_vars is not None:
        sessions.MAX_ENV_VARS = max_env_vars
    else:
        sessions.MAX_ENV_VARS = int(
            os.environ.get("BASH_MCP_SESSION_MAX_ENV_VARS", "256")
        )


def _reset_limit(n: int = 8) -> None:
    """Reset the concurrency limit (matches test_concurrency.py helper)."""
    concurrency._LIMIT = threading.BoundedSemaphore(n)
    concurrency._ACTIVE = 0
    concurrency._ACTIVE_LOCK = threading.Lock()


@pytest.fixture(autouse=True)
def _clean():
    _reset_sessions()
    _reset_limit()
    yield
    _reset_sessions()


# --- create / get / destroy / list ---


def test_create_returns_unique_id():
    """Two creates with same name produce distinct ids."""
    _reset_sessions()
    a = sessions.create(name="x")
    b = sessions.create(name="x")
    assert a.session_id != b.session_id
    assert a.session_id.startswith("s_")
    assert b.session_id.startswith("s_")


def test_create_default_name():
    """Default name is `session-<first 8 of id>`."""
    _reset_sessions()
    s = sessions.create()
    expected_prefix = s.session_id[len(sessions.SESSION_ID_PREFIX):][:8]
    assert s.name == f"session-{expected_prefix}"


def test_create_custom_name():
    _reset_sessions()
    s = sessions.create(name="my-build")
    assert s.name == "my-build"


def test_create_custom_cwd_windows_path_converted():
    """Windows-style path is auto-converted to /mnt/c/... ."""
    _reset_sessions()
    s = sessions.create(cwd=r"C:\Users\jesus")
    assert s.cwd == "/mnt/c/Users/jesus"
    assert os.path.isdir(s.cwd)


def test_create_invalid_cwd_raises():
    """cwd not under allowed root raises InvalidCwdError."""
    _reset_sessions()
    with pytest.raises(InvalidCwdError):
        sessions.create(cwd="/etc")


def test_create_nonexistent_cwd_raises():
    _reset_sessions()
    with pytest.raises(InvalidCwdError):
        sessions.create(cwd="/tmp/no-such-dir-xyz-12345")


def test_create_long_name_rejected():
    _reset_sessions()
    with pytest.raises(ValueError, match="exceeds"):
        sessions.create(name="x" * 65)


def test_get_existing_returns_state():
    _reset_sessions()
    s = sessions.create(name="alpha")
    got = sessions.get(s.session_id)
    assert got is not None
    assert got.session_id == s.session_id
    assert got.name == "alpha"


def test_get_unknown_returns_none():
    _reset_sessions()
    fake = "s_" + uuid.uuid4().hex.upper()
    assert sessions.get(fake) is None


def test_get_malformed_id_returns_none():
    _reset_sessions()
    assert sessions.get("not-a-real-id") is None
    assert sessions.get("") is None
    assert sessions.get("s_xyz") is None  # wrong hex length


def test_destroy_existing_returns_freed_count():
    _reset_sessions()
    s = sessions.create(name="x")
    sessions.apply_state_update(s.session_id, "export FOO=bar", 0)
    freed = sessions.destroy(s.session_id)
    assert freed == 1
    assert sessions.get(s.session_id) is None


def test_destroy_unknown_returns_zero():
    _reset_sessions()
    fake = "s_" + uuid.uuid4().hex.upper()
    assert sessions.destroy(fake) == 0


def test_destroy_twice_second_returns_zero():
    _reset_sessions()
    s = sessions.create()
    assert sessions.destroy(s.session_id) == 0  # empty env
    assert sessions.destroy(s.session_id) == 0  # already gone


def test_list_empty_returns_empty_list():
    _reset_sessions()
    assert sessions.list_active() == []


def test_list_after_multiple_creates():
    _reset_sessions()
    a = sessions.create(name="a")
    time.sleep(0.01)
    b = sessions.create(name="b")
    items = sessions.list_active()
    assert len(items) == 2
    # most-recent first
    assert items[0]["name"] == "b"
    assert items[1]["name"] == "a"
    assert items[0]["session_id"] == b.session_id


# --- session_run semantics via apply_state_update ---


def test_run_in_session_uses_session_cwd():
    """pwd run inside a session reflects session.cwd."""
    _reset_sessions()
    s = sessions.create(name="x", cwd="/tmp")
    sessions.apply_state_update(s.session_id, "pwd", 0)
    state = sessions.get(s.session_id)
    # state.cwd doesn't change because pwd doesn't trigger cd parsing.
    assert state.cwd == "/tmp"


def test_run_cd_simple_persists():
    _reset_sessions()
    s = sessions.create(name="x", cwd="/tmp")
    sessions.apply_state_update(s.session_id, "cd /tmp && pwd", 0)
    state = sessions.get(s.session_id)
    assert state.cwd == "/tmp"


def test_run_cd_to_disallowed_root_does_not_persist():
    """`cd /` from /tmp → /, which fails allowlist → no persist."""
    _reset_sessions()
    s = sessions.create(name="x", cwd="/tmp")
    sessions.apply_state_update(s.session_id, "cd /", 0)
    state = sessions.get(s.session_id)
    # / is not under any allowed root, so the update is silently dropped.
    assert state.cwd == "/tmp"


def test_run_cd_chain_last_wins():
    _reset_sessions()
    s = sessions.create(name="x", cwd="/home")
    sessions.apply_state_update(s.session_id, "cd /tmp && cd /home/jbecerra", 0)
    state = sessions.get(s.session_id)
    assert state.cwd == "/home/jbecerra"


def test_run_cd_quoted():
    _reset_sessions()
    s = sessions.create(name="x", cwd="/tmp")
    sessions.apply_state_update(s.session_id, 'cd "/tmp"', 0)
    state = sessions.get(s.session_id)
    assert state.cwd == "/tmp"


def test_run_cd_failed_command_does_not_persist():
    """If exit_code != 0, nothing changes (even valid cd)."""
    _reset_sessions()
    s = sessions.create(name="x", cwd="/tmp")
    sessions.apply_state_update(s.session_id, "false && cd /home/jbecerra", 1)
    state = sessions.get(s.session_id)
    assert state.cwd == "/tmp"
    # But last_used_at IS updated even on failure (so list_active stays useful).
    assert state.last_used_at >= s.created_at


def test_run_export_persists_across_calls():
    """export FOO=bar; next call sees FOO=bar in session.env."""
    _reset_sessions()
    s = sessions.create(name="x")
    sessions.apply_state_update(s.session_id, "export FOO=bar", 0)
    state = sessions.get(s.session_id)
    assert state.env.get("FOO") == "bar"


def test_run_export_quoted_single():
    _reset_sessions()
    s = sessions.create()
    sessions.apply_state_update(s.session_id, "export FOO='hello world'", 0)
    assert sessions.get(s.session_id).env["FOO"] == "hello world"


def test_run_export_quoted_double():
    _reset_sessions()
    s = sessions.create()
    sessions.apply_state_update(s.session_id, 'export FOO="hello world"', 0)
    assert sessions.get(s.session_id).env["FOO"] == "hello world"


def test_run_export_overwrites():
    _reset_sessions()
    s = sessions.create()
    sessions.apply_state_update(s.session_id, "export FOO=1; export FOO=2", 0)
    assert sessions.get(s.session_id).env["FOO"] == "2"


def test_run_unset_removes_from_session():
    _reset_sessions()
    s = sessions.create()
    sessions.apply_state_update(s.session_id, "export X=y", 0)
    sessions.apply_state_update(s.session_id, "unset X", 0)
    assert "X" not in sessions.get(s.session_id).env


def test_run_unset_unknown_is_noop():
    _reset_sessions()
    s = sessions.create()
    # Should not raise even though NOT_SET was never exported.
    sessions.apply_state_update(s.session_id, "unset NOT_SET", 0)
    state = sessions.get(s.session_id)
    assert state.env == {}


def test_run_failed_command_does_not_persist_env():
    """`false; export X=y` → exit_code=1 → no env persistence."""
    _reset_sessions()
    s = sessions.create()
    sessions.apply_state_update(s.session_id, "false; export X=y", 1)
    assert sessions.get(s.session_id).env == {}


def test_session_env_does_not_leak_between_sessions():
    _reset_sessions()
    a = sessions.create(name="a")
    b = sessions.create(name="b")
    sessions.apply_state_update(a.session_id, "export FOO=fromA", 0)
    assert sessions.get(b.session_id).env == {}
    # but a sees FOO
    assert sessions.get(a.session_id).env["FOO"] == "fromA"


def test_session_cwd_does_not_leak_between_sessions():
    _reset_sessions()
    a = sessions.create(name="a", cwd="/tmp")
    b = sessions.create(name="b", cwd="/tmp")
    sessions.apply_state_update(a.session_id, "cd /home/jbecerra", 0)
    assert sessions.get(a.session_id).cwd == "/home/jbecerra"
    assert sessions.get(b.session_id).cwd == "/tmp"


def test_session_env_cap_enforced():
    """With MAX_ENV_VARS=3, only 3 distinct exports persist; the rest are dropped."""
    _reset_sessions(max_env_vars=3)
    s = sessions.create()
    for i in range(5):
        sessions.apply_state_update(
            s.session_id, f"export V{i}=v{i}", 0
        )
    state = sessions.get(s.session_id)
    assert len(state.env) == 3
    # First 3 persist (V0, V1, V2); V3 and V4 dropped.
    assert "V0" in state.env
    assert "V1" in state.env
    assert "V2" in state.env
    assert "V3" not in state.env
    assert "V4" not in state.env


def test_session_env_cap_does_not_block_overwrite():
    """Overwriting an existing key doesn't count against the cap, but a
    NEW distinct key past the cap is dropped."""
    _reset_sessions(max_env_vars=2)
    s = sessions.create()
    sessions.apply_state_update(s.session_id, "export FOO=1", 0)
    sessions.apply_state_update(s.session_id, "export FOO=2", 0)
    assert sessions.get(s.session_id).env["FOO"] == "2"
    # Adding a 2nd distinct key fills the cap (count == 2).
    sessions.apply_state_update(s.session_id, "export BAR=b", 0)
    state = sessions.get(s.session_id)
    assert state.env == {"FOO": "2", "BAR": "b"}
    # Now adding a 3rd distinct key must be dropped (at cap).
    sessions.apply_state_update(s.session_id, "export BAZ=z", 0)
    state = sessions.get(s.session_id)
    assert "BAZ" not in state.env
    assert state.env == {"FOO": "2", "BAR": "b"}


def test_session_run_respects_concurrency_limit():
    """Parallel session runs respect BASH_MCP_MAX_CONCURRENT."""
    _reset_sessions()
    _reset_limit(3)  # small limit for fast test
    from bash_mcp.executor import run as exec_run

    s = sessions.create(name="x")
    in_flight: list[str] = []
    lock = threading.Lock()
    barrier = threading.Event()

    def worker(cmd_label: str) -> None:
        # Apply the same shape as bash_mcp_session_run, but with our
        # concurrency.slot() directly.
        with concurrency.slot():
            with lock:
                in_flight.append(cmd_label)
            # Sleep so we can observe concurrency
            barrier.wait(timeout=1.0)
            with lock:
                in_flight.remove(cmd_label)

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    # Give the first 3 a moment to enter
    time.sleep(0.1)
    assert len(in_flight) <= 3, f"expected ≤ 3 in-flight, got {len(in_flight)}"
    barrier.set()
    for t in threads:
        t.join(timeout=2.0)


def test_apply_state_update_after_destroy_is_noop():
    """If session is destroyed between runs, apply silently no-ops."""
    _reset_sessions()
    s = sessions.create()
    sessions.destroy(s.session_id)
    # Should not raise even though session is gone.
    sessions.apply_state_update(s.session_id, "export X=y", 0)
    assert sessions.get(s.session_id) is None


def test_snapshot_for_run_missing_session():
    """snapshot_for_run returns (None, None) when session doesn't exist."""
    _reset_sessions()
    fake = "s_" + uuid.uuid4().hex.upper()
    cwd, env = sessions.snapshot_for_run(fake)
    assert cwd is None
    assert env is None


def test_snapshot_for_run_malformed_id():
    _reset_sessions()
    cwd, env = sessions.snapshot_for_run("not-valid")
    assert cwd is None
    assert env is None


def test_snapshot_for_run_returns_copy_of_env():
    """Mutating the snapshot must not affect the stored session.env."""
    _reset_sessions()
    s = sessions.create()
    sessions.apply_state_update(s.session_id, "export X=y", 0)
    cwd, env = sessions.snapshot_for_run(s.session_id)
    assert cwd is not None
    assert env == {"X": "y"}
    env["X"] = "MUTATED"
    env["Z"] = "NEW"
    # Original session.env unchanged.
    assert sessions.get(s.session_id).env == {"X": "y"}


# --- 4 server tools (via direct calls, not via MCP) ---


def _call(name: str):
    """Return the underlying callable for a FastMCP-registered tool.

    FastMCP wraps each `@mcp.tool` function in a `FunctionTool` object on
    registration. When you import the function by name (e.g. for unit
    tests), you get the wrapper, which is not directly callable. This
    helper digs out the underlying function so unit tests can call it
    directly with Python kwargs.
    """
    from bash_mcp import server as _server

    tool = _server.mcp._tool_manager._tools[name]
    # FunctionTool exposes the underlying function via .fn
    return tool.fn





def test_session_create_tool_returns_envelope():
    """bash_mcp_session_create returns success envelope with audit_id."""
    _create = _call("bash_mcp_session_create")
    out = _create(name="alpha")
    assert out["name"] == "alpha"
    assert out["session_id"].startswith("s_")
    assert out["env_count"] == 0
    assert "audit_id" in out


def test_session_create_tool_invalid_cwd_returns_error_envelope():
    _create = _call("bash_mcp_session_create")
    out = _create(cwd="/etc")
    assert "error" in out
    assert out["error"]["code"] == "INVALID_CWD"
    assert "audit_id" in out


def test_session_create_tool_long_name_returns_error_envelope():
    _create = _call("bash_mcp_session_create")
    out = _create(name="x" * 65)
    assert "error" in out
    assert out["error"]["code"] == "INVALID_ARGUMENT"


def test_session_destroy_tool_existing_ok():
    _create = _call("bash_mcp_session_create")
    _destroy = _call("bash_mcp_session_destroy")
    created = _create(name="alpha")
    sid = created["session_id"]
    out = _destroy(sid)
    assert out.get("ok") is True
    assert out["session_id"] == sid
    assert "audit_id" in out


def test_session_destroy_tool_missing_returns_error_envelope():
    _destroy = _call("bash_mcp_session_destroy")
    out = _destroy("s_" + uuid.uuid4().hex.upper())
    assert "error" in out
    assert out["error"]["code"] == "SESSION_NOT_FOUND"


def test_session_list_tool_returns_summaries():
    _create = _call("bash_mcp_session_create")
    _list = _call("bash_mcp_session_list")
    _create(name="a")
    _create(name="b")
    result = _list()
    # v0.7.1 contract: {sessions: [...], count: N}
    if isinstance(result, dict) and "sessions" in result:
        items = result["sessions"]
    else:
        items = result
    assert len(items) == 2
    names = {i["name"] for i in items}
    assert names == {"a", "b"}


def test_session_run_tool_persists_cwd():
    """End-to-end: cd /tmp; pwd reflects /tmp; next pwd still /tmp."""
    _create = _call("bash_mcp_session_create")
    _run = _call("bash_mcp_session_run")
    created = _create(name="cwdtest", cwd="/home/jbecerra")
    sid = created["session_id"]
    r1 = _run(sid, "cd /tmp && pwd")
    assert r1["exit_code"] == 0
    assert "/tmp" in r1["stdout"]
    assert r1["cwd"] == "/tmp"
    # Second call: cwd should persist.
    r2 = _run(sid, "pwd")
    assert r2["exit_code"] == 0
    assert "/tmp" in r2["stdout"]
    assert r2["cwd"] == "/tmp"


def test_session_run_tool_persists_env():
    """export FOO=bar; next echo $FOO shows bar."""
    _create = _call("bash_mcp_session_create")
    _run = _call("bash_mcp_session_run")
    created = _create(name="envtest")
    sid = created["session_id"]
    r1 = _run(sid, "export FOO=hello")
    assert r1["exit_code"] == 0
    assert "FOO" in r1["env_keys"]
    r2 = _run(sid, "echo $FOO")
    assert r2["exit_code"] == 0
    assert r2["stdout"].strip() == "hello"


def test_session_run_tool_returns_audit_id_and_session_state():
    _create = _call("bash_mcp_session_create")
    _run = _call("bash_mcp_session_run")
    created = _create(name="audit")
    sid = created["session_id"]
    r = _run(sid, "echo hi")
    assert "audit_id" in r
    assert r["session_id"] == sid
    assert "cwd" in r
    assert "env_keys" in r


def test_session_run_tool_rejects_hard_denylist():
    _create = _call("bash_mcp_session_create")
    _run = _call("bash_mcp_session_run")
    created = _create(name="deny")
    sid = created["session_id"]
    r = _run(sid, "rm -rf /")
    assert "error" in r
    assert r["error"]["code"] == "FORBIDDEN_COMMAND"


def test_session_run_tool_dangerous_requires_override():
    _create = _call("bash_mcp_session_create")
    _run = _call("bash_mcp_session_run")
    created = _create(name="danger")
    sid = created["session_id"]
    # `kill -9 1` is in the SOFT denylist. Without override → rejected.
    # With override → runs but fails (EPERM on PID 1) with non-zero exit;
    # the test only requires that the call is no longer rejected.
    r = _run(sid, "kill -9 1", dangerous=False)
    assert "error" in r
    assert r["error"]["code"] == "DANGEROUS_COMMAND_REQUIRES_OVERRIDE"
    r2 = _run(sid, "kill -9 1", dangerous=True)
    assert "error" not in r2
    # exit_code is non-zero (EPERM) but the call itself succeeded.
    assert "exit_code" in r2
    assert r2["exit_code"] != 0


def test_session_run_tool_invalid_id_returns_error_envelope():
    _run = _call("bash_mcp_session_run")
    r = _run("not-a-real-id", "echo hi")
    assert "error" in r
    assert r["error"]["code"] == "SESSION_NOT_FOUND"
    assert "audit_id" in r


def test_session_run_tool_rejects_malformed_session_id():
    """Non-matching session_id format (e.g. `echo`) → SESSION_NOT_FOUND."""
    _run = _call("bash_mcp_session_run")
    r = _run("echo", "echo hi")
    assert "error" in r
    assert r["error"]["code"] == "SESSION_NOT_FOUND"


def test_session_run_tool_state_lost_on_simulated_restart():
    """Sessions live in process memory — verify by spawning a subprocess
    that imports the module fresh and sees no sessions."""
    import subprocess
    import sys

    # Create a session in this process.
    _create = _call("bash_mcp_session_create")
    created = _create(name="ephemeral")
    sid = created["session_id"]
    assert sessions.get(sid) is not None

    # Spawn a fresh Python process and verify the session is NOT visible.
    code = (
        "from bash_mcp import sessions; "
        f"import sys; "
        f"sys.exit(0 if sessions.get('{sid}') is None else 1)"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env={**os.environ, "PATH": os.environ.get("PATH", "")},
    )
    assert r.returncode == 0, (
        f"expected fresh subprocess to not see session, but it did.\n"
        f"stdout={r.stdout}\nstderr={r.stderr}"
    )
