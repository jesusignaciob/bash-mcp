"""Server-level tests for the v0.9 session persistence wiring.

The server is what turns the on-disk store into a live registry: it must
restore the previous run's sessions on boot and report the state of the
persistence layer in `bash_mcp_status`.
"""

from __future__ import annotations

from bash_mcp import server, sessions


def _status() -> dict:
    """Call the bash_mcp_status tool's underlying function.

    FastMCP wraps each `@mcp.tool` function in a FunctionTool; the plain
    function lives behind `.fn` (same pattern as test_sessions.py).
    """
    return server.mcp._tool_manager._tools["bash_mcp_status"].fn()


def test_status_reports_session_persistence():
    status = _status()
    persistence = status["sessions"]["persistence"]
    assert persistence["enabled"] is True
    assert persistence["path"].endswith("sessions.db")
    assert "persisted_sessions" in persistence


def test_bootstrap_restores_persisted_sessions(monkeypatch):
    calls = []

    def fake_restore():
        calls.append(1)
        return 3

    monkeypatch.setattr(sessions, "restore", fake_restore)
    result = server._bootstrap()
    assert calls == [1]
    assert result["restored"] == 3


def test_bootstrap_reports_zero_when_there_is_nothing_to_restore(monkeypatch):
    monkeypatch.setattr(sessions, "restore", lambda: 0)
    assert server._bootstrap()["restored"] == 0


def test_bootstrap_restores_the_real_registry_when_not_stubbed():
    # The real restore() against an empty store must be a no-op that
    # leaves the registry untouched.
    assert server._bootstrap()["restored"] == 0
    assert sessions.active_count() == 0
