"""Integration tests for session persistence (v0.9).

`bash_mcp.sessions` keeps the in-memory registry; `bash_mcp.session_store`
keeps the disk copy. These tests cover the wiring between them: every
mutation of a session must reach the store, and `restore()` must bring a
previous run's sessions back — dropping the ones that are no longer usable.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from bash_mcp import session_store, sessions


# --- helpers -------------------------------------------------------------


def _reset_registry() -> None:
    with sessions._LOCK:
        sessions._SESSIONS.clear()


@pytest.fixture(autouse=True)
def _clean_registry():
    _reset_registry()
    yield
    _reset_registry()


def _sid() -> str:
    return "s_" + uuid.uuid4().hex.upper()


# --- create / destroy ----------------------------------------------------


def test_create_persists_the_session():
    state = sessions.create(name="persisted", cwd="/tmp")
    rows = session_store.load_all()
    assert len(rows) == 1
    assert rows[0]["session_id"] == state.session_id
    assert rows[0]["name"] == "persisted"
    assert rows[0]["cwd"] == state.cwd
    assert rows[0]["env"] == {}


def test_destroy_removes_the_persisted_row():
    state = sessions.create(name="gone-soon", cwd="/tmp")
    assert session_store.count() == 1
    sessions.destroy(state.session_id)
    assert session_store.count() == 0


def test_apply_state_update_persists_cwd_and_env():
    state = sessions.create(name="moves", cwd="/tmp")
    sessions.apply_state_update(
        state.session_id, "cd /home/jbecerra && export FOO=bar", exit_code=0
    )
    rows = session_store.load_all()
    assert len(rows) == 1
    assert rows[0]["cwd"] == "/home/jbecerra"
    assert rows[0]["env"] == {"FOO": "bar"}


def test_apply_state_update_on_failure_persists_last_used_at():
    state = sessions.create(name="fails", cwd="/tmp")
    before = time.time()
    time.sleep(0.01)
    sessions.apply_state_update(state.session_id, "false", exit_code=1)
    rows = session_store.load_all()
    assert rows[0]["last_used_at"] >= before


def test_persistence_disabled_keeps_sessions_in_memory_only(monkeypatch):
    monkeypatch.setenv("BASH_MCP_SESSION_PERSIST", "0")
    state = sessions.create(name="ephemeral", cwd="/tmp")
    assert state.session_id in sessions._SESSIONS
    assert session_store.count() == 0


# --- restore -------------------------------------------------------------


def test_restore_repopulates_the_registry():
    sid = _sid()
    session_store.save(sid, "restored", "/tmp", {"A": "1"}, 100.0, 200.0)
    _reset_registry()
    assert sessions.active_count() == 0

    restored = sessions.restore()

    assert restored == 1
    assert sessions.active_count() == 1
    state = sessions.get(sid)
    assert state is not None
    assert state.name == "restored"
    assert state.cwd == "/tmp"
    assert state.env == {"A": "1"}


def test_restore_returns_zero_when_the_store_is_empty():
    assert sessions.restore() == 0


def test_restore_drops_a_session_whose_cwd_no_longer_exists():
    sid = _sid()
    missing = f"/tmp/bash-mcp-gone-{uuid.uuid4().hex}"
    session_store.save(sid, "stale", missing, {}, 100.0, 200.0)

    restored = sessions.restore()

    assert restored == 0
    assert sessions.get(sid) is None
    # the unusable row is dropped from the store too
    assert session_store.count() == 0


def test_restore_drops_a_session_outside_the_allowed_roots():
    sid = _sid()
    session_store.save(sid, "sandboxed", "/etc", {}, 100.0, 200.0)

    restored = sessions.restore()

    assert restored == 0
    assert sessions.get(sid) is None
    assert session_store.count() == 0


def test_restore_drops_a_row_with_a_malformed_session_id():
    session_store.save("not-a-session-id", "broken", "/tmp", {}, 100.0, 200.0)

    restored = sessions.restore()

    assert restored == 0
    assert sessions.active_count() == 0
    assert session_store.count() == 0


def test_restore_truncates_env_beyond_the_cap(monkeypatch):
    monkeypatch.setattr(sessions, "MAX_ENV_VARS", 3)
    sid = _sid()
    big = {f"V{i}": str(i) for i in range(10)}
    session_store.save(sid, "fat", "/tmp", big, 100.0, 200.0)

    restored = sessions.restore()

    assert restored == 1
    state = sessions.get(sid)
    assert state is not None
    assert len(state.env) == 3


def test_restore_is_a_no_op_when_persistence_is_disabled(monkeypatch):
    monkeypatch.setenv("BASH_MCP_SESSION_PERSIST", "0")
    session_store.save(_sid(), "ghost", "/tmp", {}, 100.0, 200.0)
    assert sessions.restore() == 0
    assert sessions.active_count() == 0


# --- idle eviction -------------------------------------------------------


def test_evict_idle_deletes_the_persisted_row(monkeypatch):
    monkeypatch.setattr(sessions, "IDLE_TIMEOUT_S", 1)
    state = sessions.create(name="idle", cwd="/tmp")
    assert session_store.count() == 1

    # make it look stale
    with sessions._LOCK:
        sessions._SESSIONS[state.session_id].last_used_at = time.time() - 3600

    evicted = sessions._evict_idle()

    assert evicted == 1
    assert session_store.count() == 0


# --- status --------------------------------------------------------------


def test_persistence_status_reports_enabled_path_and_count():
    sessions.create(name="counted", cwd="/tmp")
    status = sessions.persistence_status()
    assert status["enabled"] is True
    assert status["path"].endswith("sessions.db")
    assert status["persisted_sessions"] == 1


def test_persistence_status_reports_disabled(monkeypatch):
    monkeypatch.setenv("BASH_MCP_SESSION_PERSIST", "0")
    status = sessions.persistence_status()
    assert status["enabled"] is False
    assert status["persisted_sessions"] == 0
