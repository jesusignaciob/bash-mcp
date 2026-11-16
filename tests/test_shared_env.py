"""Unit tests for shared session environments (v0.11).

A shared env is a named dict that several sessions can subscribe to. It is
re-read on every run, so changing it propagates live to every subscribed
session. The values never leave the server: the listing exposes keys and
counts only.
"""

from __future__ import annotations

import pytest

from bash_mcp import session_store, sessions


def _reset_sessions() -> None:
    with sessions._LOCK:
        sessions._SESSIONS.clear()


@pytest.fixture(autouse=True)
def _clean():
    _reset_sessions()
    yield
    _reset_sessions()


# --- binding ---


def test_bind_shared_env_attaches_the_env_to_the_session():
    session_store.save_shared_env("api", {"TOKEN": "t1"})
    session = sessions.create(name="s", cwd="/tmp")

    sessions.bind_shared_env(session.session_id, "api")

    assert sessions.get(session.session_id).shared_env == "api"
    assert session_store.load_bindings() == {session.session_id: "api"}


def test_bind_shared_env_replaces_the_previous_one():
    """One shared env per session: rebinding replaces it."""
    session_store.save_shared_env("api", {"A": "1"})
    session_store.save_shared_env("other", {"B": "2"})
    session = sessions.create(name="s", cwd="/tmp")
    sessions.bind_shared_env(session.session_id, "api")

    sessions.bind_shared_env(session.session_id, "other")

    assert sessions.get(session.session_id).shared_env == "other"
    assert session_store.load_bindings() == {session.session_id: "other"}


def test_unbind_shared_env_detaches_it():
    session_store.save_shared_env("api", {"A": "1"})
    session = sessions.create(name="s", cwd="/tmp")
    sessions.bind_shared_env(session.session_id, "api")

    sessions.bind_shared_env(session.session_id, None)

    assert sessions.get(session.session_id).shared_env is None
    assert session_store.load_bindings() == {}


def test_bind_to_a_missing_env_raises():
    session = sessions.create(name="s", cwd="/tmp")

    with pytest.raises(sessions.SharedEnvNotFoundError):
        sessions.bind_shared_env(session.session_id, "nope")


def test_bind_on_a_missing_session_raises():
    session_store.save_shared_env("api", {"A": "1"})

    with pytest.raises(ValueError):
        sessions.bind_shared_env("s_" + "A" * 32, "api")


def test_create_with_shared_env_binds_it():
    session_store.save_shared_env("api", {"TOKEN": "t1"})

    session = sessions.create(name="s", cwd="/tmp", shared_env="api")

    assert session.shared_env == "api"
    assert session_store.load_bindings() == {session.session_id: "api"}


def test_create_with_a_missing_shared_env_raises():
    with pytest.raises(sessions.SharedEnvNotFoundError):
        sessions.create(name="s", cwd="/tmp", shared_env="nope")


# --- the live layering ---


def test_run_environment_layers_shared_under_local():
    """os.environ < shared env < the session's own env."""
    session_store.save_shared_env("api", {"TOKEN": "shared", "ONLY": "shared"})
    session = sessions.create(name="s", cwd="/tmp", shared_env="api")
    sessions.apply_state_update(
        session.session_id, "export TOKEN=local", exit_code=0
    )

    cwd, env, shared_name = sessions.run_environment(session.session_id)

    assert shared_name == "api"
    assert env["TOKEN"] == "local"     # the session's own env wins
    assert env["ONLY"] == "shared"     # the shared env fills the rest


def test_run_environment_is_live():
    """Changing the shared env changes what the next run sees."""
    session_store.save_shared_env("api", {"TOKEN": "t1"})
    session = sessions.create(name="s", cwd="/tmp", shared_env="api")

    _cwd, env, _name = sessions.run_environment(session.session_id)
    assert env["TOKEN"] == "t1"

    session_store.save_shared_env("api", {"TOKEN": "t2"})
    _cwd, env, _name = sessions.run_environment(session.session_id)
    assert env["TOKEN"] == "t2"


def test_run_environment_of_a_session_without_binding_has_no_shared_values():
    session_store.save_shared_env("api", {"TOKEN": "t1"})
    session = sessions.create(name="s", cwd="/tmp")

    _cwd, env, shared_name = sessions.run_environment(session.session_id)

    assert shared_name is None
    assert "TOKEN" not in env


def test_run_environment_of_a_missing_session_returns_none():
    assert sessions.run_environment("s_" + "A" * 32) is None
    assert sessions.run_environment("not-a-session") is None


# --- listing ---


def test_list_active_reports_the_shared_env_and_its_keys():
    session_store.save_shared_env("api", {"TOKEN": "t1", "REGION": "eu"})
    session = sessions.create(name="s", cwd="/tmp", shared_env="api")

    listed = sessions.list_active()

    assert len(listed) == 1
    assert listed[0]["shared_env"] == "api"
    assert listed[0]["shared_env_keys"] == ["REGION", "TOKEN"]
    assert "t1" not in repr(listed)


# --- lifecycle ---


def test_destroy_removes_the_binding():
    session_store.save_shared_env("api", {"A": "1"})
    session = sessions.create(name="s", cwd="/tmp", shared_env="api")

    sessions.destroy(session.session_id)

    assert session_store.load_bindings() == {}


def test_restore_reattaches_the_bindings():
    session_store.save_shared_env("api", {"TOKEN": "t1"})
    session = sessions.create(name="s", cwd="/tmp", shared_env="api")
    session_id = session.session_id
    # A restart leaves the row on disk with nothing in memory.
    with sessions._LOCK:
        sessions._SESSIONS.clear()

    restored = sessions.restore()

    assert restored == 1
    assert sessions.get(session_id).shared_env == "api"


def test_restore_drops_bindings_whose_env_no_longer_exists():
    """An orphan binding (its env was deleted) is discarded, not restored."""
    session = sessions.create(name="s", cwd="/tmp")
    session_id = session.session_id
    session_store.bind_session_env(session_id, "gone")
    # A restart leaves the row on disk with nothing in memory.
    with sessions._LOCK:
        sessions._SESSIONS.clear()

    restored = sessions.restore()

    assert restored == 1
    assert sessions.get(session_id).shared_env is None
    assert session_store.load_bindings() == {}
