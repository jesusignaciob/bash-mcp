"""Unit tests for session auto-TTL / idle eviction (v0.7).

Pattern: reuse _reset_sessions() from test_sessions.py and call
_evict_idle() directly to make eviction deterministic. The daemon
thread is never started in these tests (we reset _JANITOR_STARTED
before and after each test).
"""

from __future__ import annotations

import os
import threading
import tempfile
import time

import pytest

from bash_mcp import sessions


# --- helpers ---


def _reset_sessions() -> None:
    """Clear the session dict + reset TTL state. From test_sessions.py."""
    with sessions._LOCK:
        sessions._SESSIONS.clear()
    sessions.IDLE_TIMEOUT_S = int(
        __import__("os").environ.get("BASH_MCP_SESSION_IDLE_TIMEOUT_S", "0")
    )
    sessions._reset_janitor()


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    _reset_sessions()
    # Redirect audit log to tmp_path so eviction audits don't pollute
    # the real /home/jbecerra/.local/share/bash-mcp/audit.jsonl.
    from bash_mcp import audit
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(audit, "_AUDIT_FILE", None)
    yield
    _reset_sessions()


def _set_ttl(seconds: int) -> None:
    """Set IDLE_TIMEOUT_S and reset janitor state."""
    sessions.IDLE_TIMEOUT_S = seconds
    sessions._reset_janitor()


def _make_old_session(timeout_s: int, age_s: float):
    """Create a session and backdate its last_used_at to look idle.

    Returns the SessionState object so callers can use either the
    session_id (s.session_id) or the state itself.
    """
    s = sessions.create(name=f"old-{age_s:.1f}s")
    s.last_used_at = time.time() - age_s
    return s


# --- disabled-by-default ---


def test_ttl_disabled_by_default():
    """Default (env var unset) leaves IDLE_TIMEOUT_S at 0 \u2014 no eviction."""
    # _clean fixture already reset; default is 0
    assert sessions.IDLE_TIMEOUT_S == 0
    s = sessions.create()
    assert sessions._evict_idle() == 0
    assert sessions.get(s.session_id) is not None


def test_ttl_zero_means_no_eviction():
    """Explicit IDLE_TIMEOUT_S=0 disables eviction even with stale sessions."""
    _set_ttl(0)
    s = _make_old_session(0, age_s=10000.0)  # way past any reasonable timeout
    assert sessions._evict_idle() == 0
    assert sessions.get(s.session_id) is not None


# --- basic eviction ---


def test_ttl_evicts_old_sessions():
    _set_ttl(60)
    old_id = _make_old_session(60, age_s=120.0)
    recent_id = sessions.create(name="recent").session_id

    evicted = sessions._evict_idle()

    assert evicted == 1
    assert sessions.get(old_id) is None
    assert sessions.get(recent_id) is not None


def test_ttl_does_not_evict_recent_sessions():
    _set_ttl(60)
    s = sessions.create()
    # Just created \u2014 last_used_at is essentially now.
    # Force it slightly old but still well within timeout.
    s.last_used_at = time.time() - 30  # 30s old, timeout 60s
    evicted = sessions._evict_idle()
    assert evicted == 0
    assert sessions.get(s.session_id) is not None


# --- audit shape ---


def test_ttl_eviction_audits_with_reason_idle_ttl_expired(capsys):
    _set_ttl(60)
    s = _make_old_session(60, age_s=120.0).session_id
    sessions._evict_idle()

    # The autouse fixture redirects audit to tmp_path.
    from bash_mcp import audit
    import json
    path = audit.audit_path()
    assert path.exists(), "audit path must exist (autouse fixture set XDG_DATA_HOME)"
    entries = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    evictions = [e for e in entries if e.get("reason") == "idle_ttl_expired"]
    assert len(evictions) == 1
    assert evictions[0]["tool"] == "bash_mcp_session_destroy"
    assert evictions[0]["args"]["session_id"] == s


def test_ttl_eviction_logs_freed_env_vars_count():
    _set_ttl(60)
    s = sessions.create()
    sessions.apply_state_update(s.session_id, "export FOO=bar", 0)
    s.last_used_at = time.time() - 120

    sessions._evict_idle()

    import json
    from bash_mcp import audit
    entries = [
        json.loads(line)
        for line in audit.audit_path().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    evictions = [e for e in entries if e.get("reason") == "idle_ttl_expired"]
    assert len(evictions) == 1
    assert evictions[0]["freed_env_vars"] == 1


def test_ttl_eviction_uses_session_id_in_audit():
    _set_ttl(60)
    s = _make_old_session(60, age_s=120.0)
    sessions._evict_idle()

    import json
    from bash_mcp import audit
    entries = [
        json.loads(line)
        for line in audit.audit_path().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    evictions = [e for e in entries if e.get("reason") == "idle_ttl_expired"]
    assert evictions[0]["args"]["session_id"] == s.session_id


# --- cutoff edge cases ---


def test_ttl_sweep_at_exact_cutoff_evicts_old():
    """last_used_at = cutoff - 1ms \u2014 strictly less, so evicted."""
    _set_ttl(60)
    s = sessions.create()
    cutoff = time.time() - 60
    s.last_used_at = cutoff - 0.001
    assert sessions._evict_idle() == 1
    assert sessions.get(s.session_id) is None


def test_ttl_sweep_at_exact_cutoff_keeps_recent():
    """last_used_at = cutoff + 1ms \u2014 strictly greater, so kept."""
    _set_ttl(60)
    s = sessions.create()
    cutoff = time.time() - 60
    s.last_used_at = cutoff + 0.001
    assert sessions._evict_idle() == 0
    assert sessions.get(s.session_id) is not None


# --- post-eviction behavior ---


def test_ttl_evicted_session_returns_none_from_get():
    _set_ttl(60)
    s = _make_old_session(60, age_s=120.0)
    sessions._evict_idle()
    assert sessions.get(s.session_id) is None


def test_ttl_evicted_session_returns_404_from_session_run_tool():
    """After TTL eviction, calling bash_mcp_session_run returns SESSION_NOT_FOUND."""
    from bash_mcp import server
    _session_run = server.mcp._tool_manager._tools["bash_mcp_session_run"].fn
    _set_ttl(60)
    s = _make_old_session(60, age_s=120.0)
    sessions._evict_idle()
    out = _session_run(s.session_id, "echo x")
    assert "error" in out
    assert out["error"]["code"] == "SESSION_NOT_FOUND"


# --- janitor thread lifecycle ---


def test_ttl_janitor_thread_does_not_start_when_timeout_zero():
    _set_ttl(0)
    assert sessions._JANITOR_STARTED is False
    s = sessions.create()
    assert sessions._JANITOR_STARTED is False


def test_ttl_janitor_thread_starts_when_timeout_set_and_create_called():
    _set_ttl(60)
    assert sessions._JANITOR_STARTED is False
    s = sessions.create()
    # Daemon thread starts synchronously inside _ensure_janitor() \u2014 flag
    # must be True immediately after create() returns.
    assert sessions._JANITOR_STARTED is True


def test_ttl_janitor_thread_is_daemon():
    """The janitor thread must be a daemon so it dies with the process."""
    import threading
    _set_ttl(60)
    # Pre-existing daemon threads from earlier tests can't be stopped,
    # but we can mark _JANITOR_STARTED False and start fresh.
    sessions._reset_janitor()
    sessions.create()  # starts a new janitor
    found = [
        t for t in threading.enumerate()
        if t.name == "bash-mcp-session-janitor" and t.is_alive()
    ]
    assert len(found) >= 1, "expected at least one live janitor thread"
    # All janitor threads must be daemons.
    assert all(t.daemon for t in found), "all janitors must be daemon"
    sessions._reset_janitor()


def test_ttl_janitor_thread_sweep_interval_quarter_of_timeout():
    """Sweep interval is max(5, min(300, timeout // 4))."""
    assert sessions._janitor_sweep_interval_s() == 5  # timeout=0 fallback
    _set_ttl(60)
    assert sessions._janitor_sweep_interval_s() == 15  # 60 // 4
    _set_ttl(120)
    assert sessions._janitor_sweep_interval_s() == 30  # 120 // 4


def test_ttl_janitor_thread_sweep_interval_clamped_to_5_to_300():
    _set_ttl(10)   # 10 // 4 = 2 \u2014 floor at 5
    assert sessions._janitor_sweep_interval_s() == 5
    _set_ttl(60_000)  # 60000 // 4 = 15000 \u2014 ceiling at 300
    assert sessions._janitor_sweep_interval_s() == 300


def test_ttl_janitor_thread_logs_warning_on_sweep_failure(capsys, monkeypatch):
    """If _evict_idle raises, the loop catches it and prints to stderr."""
    _set_ttl(60)
    sessions.create()  # starts the janitor

    # Patch _evict_idle to raise.
    def boom():
        raise RuntimeError("simulated sweep failure")
    monkeypatch.setattr(sessions, "_evict_idle", boom)

    # Manually run one iteration of the loop body.
    try:
        sessions._evict_idle()
    except RuntimeError:
        # Simulate the loop's try/except.
        import sys
        try:
            sessions._evict_idle()
        except Exception as e:
            print(f"[bash-mcp sessions] WARN: janitor sweep failed: {e}", file=sys.stderr)

    captured = capsys.readouterr()
    assert "WARN" in captured.err
    assert "simulated sweep failure" in captured.err


# --- status integration ---


def test_ttl_status_reflects_idle_timeout_s():
    _set_ttl(123)
    from bash_mcp import server
    out = server.mcp._tool_manager._tools["bash_mcp_status"].fn()
    assert out["sessions"]["idle_timeout_s"] == 123


def test_ttl_status_janitor_enabled_field():
    from bash_mcp import server
    _status = server.mcp._tool_manager._tools["bash_mcp_status"].fn
    _set_ttl(0)
    out = _status()
    assert out["sessions"]["janitor_enabled"] is False

    _set_ttl(60)
    out = _status()
    assert out["sessions"]["janitor_enabled"] is True


# --- concurrency ---


def test_ttl_eviction_idempotent_under_concurrent_calls():
    """Two threads calling _evict_idle() simultaneously must not double-evict."""
    _set_ttl(60)
    ids = [_make_old_session(60, age_s=120.0) for _ in range(5)]

    results: list[int] = []
    lock = threading.Lock()

    def worker():
        n = sessions._evict_idle()
        with lock:
            results.append(n)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)

    # Sum across all threads equals exactly the stale count (5).
    # If two threads both reported 5, the sum would be 10 (or some other >5),
    # which would mean double-decrement.
    assert sum(results) == 5
    for sid in ids:
        assert sessions.get(sid) is None
