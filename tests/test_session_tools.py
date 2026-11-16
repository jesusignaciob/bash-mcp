"""Unit tests for the v0.11 tools: session fork + shared environments.

They call the tools through the FastMCP tool manager (the pattern the
repo already uses in test_server_persistence.py), so the real server-side
code paths — argument handling, error envelopes and audit entries — are
exercised without a live server.
"""

from __future__ import annotations

import json

import pytest

from bash_mcp import audit, server, session_store, sessions


SID = "s_" + "A" * 32


@pytest.fixture(autouse=True)
def _isolated():
    """Keep the registry, the session store and the audit log out of the way."""
    with sessions._LOCK:
        sessions._SESSIONS.clear()
    from tests.conftest import _isolated_session_store  # noqa: F401  (fixture import)

    yield
    with sessions._LOCK:
        sessions._SESSIONS.clear()


@pytest.fixture(autouse=True)
def _isolated_audit(tmp_path, monkeypatch):
    """The audit log of a test run must not land in the real one."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(audit, "_AUDIT_FILE", None)
    yield
    monkeypatch.setattr(audit, "_AUDIT_FILE", None)


def _tool(name: str):
    return server.mcp._tool_manager._tools[name].fn


def _audit_entries() -> list[dict]:
    path = audit.audit_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# --- bash_mcp_session_fork ---


def test_fork_creates_an_independent_session():
    parent = sessions.create(name="parent", cwd="/tmp")
    sessions.apply_state_update(parent.session_id, "export FOO=bar", exit_code=0)

    out = _tool("bash_mcp_session_fork")(session_id=parent.session_id)

    assert out["session_id"] != parent.session_id
    assert out["forked_from"] == parent.session_id
    assert out["cwd"] == "/tmp"
    assert out["env_count"] == 1
    assert "audit_id" in out


def test_fork_copies_the_env_of_the_parent():
    parent = sessions.create(name="parent", cwd="/tmp")
    sessions.apply_state_update(parent.session_id, "export FOO=bar", exit_code=0)

    out = _tool("bash_mcp_session_fork")(session_id=parent.session_id)

    assert sessions.get(out["session_id"]).env == {"FOO": "bar"}


def test_fork_of_a_missing_session_returns_session_not_found():
    out = _tool("bash_mcp_session_fork")(session_id=SID)

    assert out["error"]["code"] == "SESSION_NOT_FOUND"


def test_fork_with_an_invalid_cwd_returns_invalid_cwd():
    parent = sessions.create(name="parent", cwd="/tmp")

    out = _tool("bash_mcp_session_fork")(session_id=parent.session_id, cwd="/etc")

    assert out["error"]["code"] == "INVALID_CWD"


def test_fork_with_a_too_long_name_returns_invalid_argument():
    parent = sessions.create(name="parent", cwd="/tmp")

    out = _tool("bash_mcp_session_fork")(
        session_id=parent.session_id, name="x" * 200
    )

    assert out["error"]["code"] == "INVALID_ARGUMENT"


def test_fork_is_audited():
    parent = sessions.create(name="parent", cwd="/tmp")

    _tool("bash_mcp_session_fork")(session_id=parent.session_id)

    entries = [e for e in _audit_entries() if e["tool"] == "bash_mcp_session_fork"]
    assert entries, _audit_entries()
    assert entries[-1]["outcome"] == "CREATED"
    assert entries[-1]["args"]["forked_from"] == parent.session_id


# --- bash_mcp_env_share ---


def test_env_share_creates_a_named_env_without_echoing_the_values():
    out = _tool("bash_mcp_env_share")(name="api", vars={"TOKEN": "super-secret"})

    assert out["name"] == "api"
    assert out["env_count"] == 1
    assert out["env_keys"] == ["TOKEN"]
    assert "super-secret" not in json.dumps(out)


def test_env_share_merges_new_keys():
    _tool("bash_mcp_env_share")(name="api", vars={"A": "1", "B": "2"})

    out = _tool("bash_mcp_env_share")(name="api", vars={"B": "9", "C": "3"})

    assert out["env_count"] == 3
    assert out["env_keys"] == ["A", "B", "C"]
    assert session_store.load_shared_env("api") == {"A": "1", "B": "9", "C": "3"}


def test_env_share_unsets_keys():
    _tool("bash_mcp_env_share")(name="api", vars={"A": "1", "B": "2"})

    out = _tool("bash_mcp_env_share")(name="api", vars={}, unset=["A"])

    assert out["env_keys"] == ["B"]
    assert session_store.load_shared_env("api") == {"B": "2"}


def test_env_share_without_changes_is_invalid_argument():
    out = _tool("bash_mcp_env_share")(name="api", vars={})

    assert out["error"]["code"] == "INVALID_ARGUMENT"


def test_env_share_never_audits_the_values():
    _tool("bash_mcp_env_share")(name="api", vars={"TOKEN": "super-secret"})

    raw = json.dumps(_audit_entries())
    assert "super-secret" not in raw
    entries = [e for e in _audit_entries() if e["tool"] == "bash_mcp_env_share"]
    assert entries[-1]["args"]["keys"] == ["TOKEN"]


# --- bash_mcp_env_list ---


def test_env_list_returns_names_and_keys_but_no_values():
    _tool("bash_mcp_env_share")(name="api", vars={"TOKEN": "super-secret"})

    out = _tool("bash_mcp_env_list")()

    assert out["count"] == 1
    entry = out["envs"][0]
    assert entry["name"] == "api"
    assert entry["env_keys"] == ["TOKEN"]
    assert "super-secret" not in json.dumps(out)


def test_env_list_reports_the_sessions_bound_to_each_env():
    _tool("bash_mcp_env_share")(name="api", vars={"A": "1"})
    session = sessions.create(name="s", cwd="/tmp")
    _tool("bash_mcp_session_bind_env")(session_id=session.session_id, env_name="api")

    out = _tool("bash_mcp_env_list")()

    assert out["envs"][0]["sessions"] == [session.session_id]


# --- bash_mcp_env_delete ---


def test_env_delete_removes_the_env_and_unbinds_the_sessions():
    _tool("bash_mcp_env_share")(name="api", vars={"A": "1"})
    session = sessions.create(name="s", cwd="/tmp")
    _tool("bash_mcp_session_bind_env")(session_id=session.session_id, env_name="api")

    out = _tool("bash_mcp_env_delete")(name="api")

    assert out["ok"] is True
    assert out["unbound_sessions"] == 1
    assert session_store.load_shared_env("api") is None
    assert sessions.get(session.session_id).shared_env is None


def test_env_delete_of_a_missing_env_returns_shared_env_not_found():
    out = _tool("bash_mcp_env_delete")(name="nope")

    assert out["error"]["code"] == "SHARED_ENV_NOT_FOUND"


# --- bash_mcp_session_bind_env ---


def test_bind_env_attaches_the_shared_env():
    _tool("bash_mcp_env_share")(name="api", vars={"A": "1"})
    session = sessions.create(name="s", cwd="/tmp")

    out = _tool("bash_mcp_session_bind_env")(
        session_id=session.session_id, env_name="api"
    )

    assert out["ok"] is True
    assert out["shared_env"] == "api"
    assert sessions.get(session.session_id).shared_env == "api"


def test_bind_env_without_a_name_unbinds():
    _tool("bash_mcp_env_share")(name="api", vars={"A": "1"})
    session = sessions.create(name="s", cwd="/tmp")
    _tool("bash_mcp_session_bind_env")(session_id=session.session_id, env_name="api")

    out = _tool("bash_mcp_session_bind_env")(session_id=session.session_id)

    assert out["ok"] is True
    assert out["shared_env"] is None
    assert sessions.get(session.session_id).shared_env is None


def test_bind_env_to_a_missing_env_returns_shared_env_not_found():
    session = sessions.create(name="s", cwd="/tmp")

    out = _tool("bash_mcp_session_bind_env")(
        session_id=session.session_id, env_name="nope"
    )

    assert out["error"]["code"] == "SHARED_ENV_NOT_FOUND"


def test_bind_env_on_a_missing_session_returns_session_not_found():
    _tool("bash_mcp_env_share")(name="api", vars={"A": "1"})

    out = _tool("bash_mcp_session_bind_env")(session_id=SID, env_name="api")

    assert out["error"]["code"] == "SESSION_NOT_FOUND"


# --- the live layering through the real tool ---


def test_session_run_applies_the_shared_env_live():
    _tool("bash_mcp_env_share")(name="api", vars={"TOKEN": "t1"})
    session = sessions.create(name="s", cwd="/tmp")
    _tool("bash_mcp_session_bind_env")(session_id=session.session_id, env_name="api")

    out = _tool("bash_mcp_session_run")(
        session_id=session.session_id, command="echo $TOKEN"
    )
    assert out["stdout"].strip() == "t1"

    # Live: the next run sees the new value.
    _tool("bash_mcp_env_share")(name="api", vars={"TOKEN": "t2"})
    out = _tool("bash_mcp_session_run")(
        session_id=session.session_id, command="echo $TOKEN"
    )
    assert out["stdout"].strip() == "t2"


def test_session_run_lets_the_session_env_win_over_the_shared_one():
    _tool("bash_mcp_env_share")(name="api", vars={"TOKEN": "shared"})
    session = sessions.create(name="s", cwd="/tmp")
    _tool("bash_mcp_session_bind_env")(session_id=session.session_id, env_name="api")
    sessions.apply_state_update(session.session_id, "export TOKEN=local", exit_code=0)

    out = _tool("bash_mcp_session_run")(
        session_id=session.session_id, command="echo $TOKEN"
    )

    assert out["stdout"].strip() == "local"


def test_session_run_does_not_leak_the_shared_env_to_other_sessions():
    _tool("bash_mcp_env_share")(name="api", vars={"TOKEN": "t1"})
    subscribed = sessions.create(name="sub", cwd="/tmp")
    plain = sessions.create(name="plain", cwd="/tmp")
    _tool("bash_mcp_session_bind_env")(
        session_id=subscribed.session_id, env_name="api"
    )

    out = _tool("bash_mcp_session_run")(
        session_id=plain.session_id, command="echo $TOKEN"
    )

    assert out["stdout"].strip() == ""


# --- status ---


def test_status_reports_the_shared_envs():
    _tool("bash_mcp_env_share")(name="api", vars={"TOKEN": "t1"})

    out = _tool("bash_mcp_status")()

    assert out["shared_envs"]["count"] == 1
    assert out["shared_envs"]["names"] == ["api"]


def test_status_lists_the_registered_tools():
    out = _tool("bash_mcp_status")()

    assert len(out["tools"]) == 17
    assert "bash_mcp_session_fork" in out["tools"]
    assert "bash_mcp_env_share" in out["tools"]
    assert "bash_mcp_env_list" in out["tools"]
    assert "bash_mcp_env_delete" in out["tools"]
    assert "bash_mcp_session_bind_env" in out["tools"]
