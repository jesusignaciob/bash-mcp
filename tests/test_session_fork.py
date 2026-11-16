"""Unit tests for session forking (v0.11).

A fork copies the parent's `cwd` and local `env` at that instant and
returns a brand-new, independent session. The parent is untouched: the
copy is a branch, not a move.
"""

from __future__ import annotations

import os

import pytest

from bash_mcp import session_store, sessions
from bash_mcp.executor import InvalidCwdError


def _reset_sessions() -> None:
    with sessions._LOCK:
        sessions._SESSIONS.clear()


@pytest.fixture(autouse=True)
def _clean():
    _reset_sessions()
    yield
    _reset_sessions()


def test_fork_copies_cwd_and_env():
    """The fork starts with the parent's cwd and env."""
    parent = sessions.create(name="parent", cwd="/tmp")
    sessions.apply_state_update(parent.session_id, "export FOO=bar", exit_code=0)

    child = sessions.fork(parent.session_id)

    assert child.session_id != parent.session_id
    assert child.cwd == parent.cwd
    assert child.env == {"FOO": "bar"}


def test_fork_leaves_the_parent_untouched():
    """Exporting in the copy does not touch the parent."""
    parent = sessions.create(name="parent", cwd="/tmp")

    child = sessions.fork(parent.session_id)
    sessions.apply_state_update(child.session_id, "export CHILD=1", exit_code=0)

    assert "CHILD" not in sessions.get(parent.session_id).env


def test_fork_is_independent_from_the_parent():
    """The copy starts with the parent's env but changes do not travel back."""
    parent = sessions.create(name="parent", cwd="/tmp")
    sessions.apply_state_update(parent.session_id, "export PARENT=1", exit_code=0)

    child = sessions.fork(parent.session_id)
    assert "PARENT" in child.env          # it is a copy

    sessions.apply_state_update(parent.session_id, "export LATER=1", exit_code=0)
    assert "LATER" not in child.env        # and the parent moves on alone


def test_fork_accepts_a_cwd_override():
    parent = sessions.create(name="parent", cwd="/tmp")

    child = sessions.fork(parent.session_id, cwd="/home/jbecerra")

    assert child.cwd == "/home/jbecerra"


def test_fork_rejects_an_invalid_cwd_override():
    parent = sessions.create(name="parent", cwd="/tmp")

    with pytest.raises(InvalidCwdError):
        sessions.fork(parent.session_id, cwd="/etc")


def test_fork_rejects_a_missing_cwd_override():
    parent = sessions.create(name="parent", cwd="/tmp")

    with pytest.raises(InvalidCwdError):
        sessions.fork(parent.session_id, cwd="/tmp/definitely-not-here-v011")


def test_fork_default_name_is_parent_plus_fork():
    parent = sessions.create(name="parent", cwd="/tmp")

    child = sessions.fork(parent.session_id)

    assert child.name == "parent-fork"


def test_fork_default_name_is_truncated():
    parent = sessions.create(name="x" * 64, cwd="/tmp")

    child = sessions.fork(parent.session_id)

    assert len(child.name) <= sessions.MAX_SESSION_NAME_LEN
    assert child.name.endswith("-fork")


def test_fork_accepts_an_explicit_name():
    parent = sessions.create(name="parent", cwd="/tmp")

    child = sessions.fork(parent.session_id, name="branch-a")

    assert child.name == "branch-a"


def test_fork_of_a_missing_session_raises():
    with pytest.raises(ValueError):
        sessions.fork("s_" + "A" * 32)


def test_fork_of_a_malformed_id_raises():
    with pytest.raises(ValueError):
        sessions.fork("not-a-session")


def test_fork_is_persisted():
    """The fork is a normal session, so it lands in the SQLite store."""
    parent = sessions.create(name="parent", cwd="/tmp")

    child = sessions.fork(parent.session_id)

    ids = {row["session_id"] for row in session_store.load_all()}
    assert child.session_id in ids


def test_fork_inherits_the_shared_env_binding():
    """The child inherits the binding (and sees it live), not a copy of it."""
    session_store.save_shared_env("api", {"TOKEN": "t1"})
    parent = sessions.create(name="parent", cwd="/tmp")
    sessions.bind_shared_env(parent.session_id, "api")

    child = sessions.fork(parent.session_id)

    assert child.shared_env == "api"
    _cwd, env, shared_name = sessions.run_environment(child.session_id)
    assert shared_name == "api"
    assert env["TOKEN"] == "t1"

    # Live: changing the shared env changes what the child sees.
    session_store.save_shared_env("api", {"TOKEN": "t2"})
    _cwd, env, _name = sessions.run_environment(child.session_id)
    assert env["TOKEN"] == "t2"


def test_fork_of_a_parent_without_shared_env_has_none():
    parent = sessions.create(name="parent", cwd="/tmp")

    child = sessions.fork(parent.session_id)

    assert child.shared_env is None
