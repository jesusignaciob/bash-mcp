"""Global test isolation for the bash-mcp suite.

Every test gets its own session-store database file, so the suite never
touches the real `~/.local/share/bash-mcp/sessions.db` and no test can
observe another test's persisted sessions.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_session_store(tmp_path, monkeypatch):
    """Point the session store at a throwaway DB for the duration of a test."""
    monkeypatch.setenv("BASH_MCP_SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("BASH_MCP_SESSION_PERSIST", "1")
    yield
