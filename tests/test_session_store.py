"""Unit tests for the SQLite-backed session store (v0.9).

The store is the persistence layer behind `bash_mcp_session_*`: it keeps one
row per session so a server restart can restore the sessions that were alive
before. It must be fail-soft — a broken database degrades to "no
persistence", never to a crash.
"""

from __future__ import annotations

import pytest

from bash_mcp import session_store


SID = "s_" + "A" * 32
SID2 = "s_" + "B" * 32


# --- empty database ---


def test_load_all_on_empty_db_returns_empty_list():
    assert session_store.load_all() == []


def test_count_on_empty_db_is_zero():
    assert session_store.count() == 0


# --- save / load round trip ---


def test_save_then_load_all_returns_the_row():
    session_store.save(SID, "demo", "/tmp", {"FOO": "bar"}, 100.0, 200.0)
    rows = session_store.load_all()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == SID
    assert row["name"] == "demo"
    assert row["cwd"] == "/tmp"
    assert row["env"] == {"FOO": "bar"}
    assert row["created_at"] == 100.0
    assert row["last_used_at"] == 200.0


def test_save_twice_same_id_updates_in_place():
    session_store.save(SID, "demo", "/tmp", {"A": "1"}, 100.0, 200.0)
    session_store.save(SID, "demo", "/var", {"A": "2"}, 100.0, 300.0)
    rows = session_store.load_all()
    assert len(rows) == 1
    assert rows[0]["cwd"] == "/var"
    assert rows[0]["env"] == {"A": "2"}
    assert rows[0]["last_used_at"] == 300.0


def test_save_keeps_multiple_sessions_apart():
    session_store.save(SID, "one", "/tmp", {}, 100.0, 200.0)
    session_store.save(SID2, "two", "/var", {}, 100.0, 200.0)
    assert session_store.count() == 2


def test_delete_removes_the_row():
    session_store.save(SID, "demo", "/tmp", {}, 100.0, 200.0)
    session_store.delete(SID)
    assert session_store.load_all() == []
    assert session_store.count() == 0


def test_delete_unknown_id_is_a_no_op():
    session_store.save(SID, "demo", "/tmp", {}, 100.0, 200.0)
    session_store.delete(SID2)
    assert session_store.count() == 1


# --- disabled persistence ---


def test_disabled_store_creates_no_file(monkeypatch, tmp_path):
    db = tmp_path / "sessions.db"
    monkeypatch.setenv("BASH_MCP_SESSION_DB_PATH", str(db))
    monkeypatch.setenv("BASH_MCP_SESSION_PERSIST", "0")
    session_store.save(SID, "demo", "/tmp", {}, 1.0, 2.0)
    assert not db.exists()
    assert session_store.load_all() == []
    assert session_store.count() == 0


def test_enabled_reports_the_env_flag(monkeypatch):
    monkeypatch.setenv("BASH_MCP_SESSION_PERSIST", "1")
    assert session_store.enabled() is True
    monkeypatch.setenv("BASH_MCP_SESSION_PERSIST", "0")
    assert session_store.enabled() is False
    monkeypatch.setenv("BASH_MCP_SESSION_PERSIST", "off")
    assert session_store.enabled() is False


# --- fail-soft on a broken database ---


def test_corrupt_db_load_returns_empty_without_raising(monkeypatch, tmp_path):
    db = tmp_path / "sessions.db"
    db.write_text("this is definitely not a sqlite database", encoding="utf-8")
    monkeypatch.setenv("BASH_MCP_SESSION_DB_PATH", str(db))
    assert session_store.load_all() == []


def test_corrupt_db_save_does_not_raise(monkeypatch, tmp_path):
    db = tmp_path / "sessions.db"
    db.write_text("this is definitely not a sqlite database", encoding="utf-8")
    monkeypatch.setenv("BASH_MCP_SESSION_DB_PATH", str(db))
    session_store.save(SID, "demo", "/tmp", {}, 1.0, 2.0)  # must not raise


def test_unwritable_db_path_does_not_raise(monkeypatch, tmp_path):
    # A path whose parent is a file, not a directory.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("BASH_MCP_SESSION_DB_PATH", str(blocker / "sessions.db"))
    session_store.save(SID, "demo", "/tmp", {}, 1.0, 2.0)
    assert session_store.load_all() == []


# --- db path resolution ---


def test_db_path_honours_the_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom.db"
    monkeypatch.setenv("BASH_MCP_SESSION_DB_PATH", str(target))
    assert session_store.db_path() == target


def test_db_path_defaults_under_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("BASH_MCP_SESSION_DB_PATH", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert session_store.db_path() == tmp_path / "bash-mcp" / "sessions.db"
