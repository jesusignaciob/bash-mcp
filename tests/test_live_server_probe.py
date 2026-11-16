"""Unit tests for the live-server probe (`tests/live_server.py`).

The e2e suite used to decide whether to run with a module-level
`pytest.mark.skipif(not _server_reachable(), ...)`. Any probe failure —
including "the server is busy serving this very test run" — silently
skipped all 24 e2e tests, and the suite still exited 0.

These tests pin the four-state decision matrix that replaces it:

    port closed                    -> SKIP_NO_SERVER    (honest skip)
    port open + handshake OK       -> RUN
    port open + handshake fails
       + nested inside the server  -> SKIP_NESTED       (honest, explicit skip)
       + not nested                -> FAIL_UNREACHABLE  (red, never a silent green)

No sockets and no network here: the probe's three inputs are monkeypatched.
"""

from __future__ import annotations

import pytest

from tests import live_server
from tests.live_server import Decision


ENDPOINT = live_server.ENDPOINT


@pytest.fixture
def probe_env(monkeypatch):
    """Neutral defaults: port open, handshake OK, not nested, no sleeping."""
    monkeypatch.setattr(live_server, "port_open", lambda *a, **k: True)
    monkeypatch.setattr(live_server, "handshake", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(live_server, "running_inside_server", lambda: False)
    monkeypatch.setattr(live_server.time, "sleep", lambda *_: None)
    return monkeypatch


# --- the matrix ---


def test_port_closed_yields_skip_no_server(probe_env, monkeypatch):
    """No server listening -> honest skip, not a failure."""
    monkeypatch.setattr(live_server, "port_open", lambda *a, **k: False)

    result = live_server.probe(ENDPOINT)

    assert result.decision is Decision.SKIP_NO_SERVER
    assert "54321" in result.reason


def test_handshake_ok_yields_run(probe_env):
    """Port open and the handshake answers -> the e2e must run."""
    result = live_server.probe(ENDPOINT)

    assert result.decision is Decision.RUN
    assert result.attempts == 1


def test_handshake_failure_not_nested_fails(probe_env, monkeypatch):
    """The server listens but does not answer, and it is not ours -> RED."""
    monkeypatch.setattr(
        live_server, "handshake", lambda *a, **k: (False, "TimeoutError: timed out")
    )

    result = live_server.probe(ENDPOINT)

    assert result.decision is Decision.FAIL_UNREACHABLE
    assert "54321" in result.reason
    assert "TimeoutError" in result.detail


def test_handshake_failure_nested_skips(probe_env, monkeypatch):
    """We are inside the server: it cannot answer while serving us."""
    monkeypatch.setattr(
        live_server, "handshake", lambda *a, **k: (False, "TimeoutError: timed out")
    )
    monkeypatch.setattr(live_server, "running_inside_server", lambda: True)

    result = live_server.probe(ENDPOINT)

    assert result.decision is Decision.SKIP_NESTED
    assert "bash-mcp" in result.reason


def test_handshake_is_retried_before_deciding(probe_env, monkeypatch):
    """A momentarily busy server may answer on a later attempt."""
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            return False, "TimeoutError: timed out"
        return True, "ok on attempt %d" % len(calls)

    monkeypatch.setattr(live_server, "handshake", flaky)

    result = live_server.probe(ENDPOINT, attempts=3, backoff=0)

    assert result.decision is Decision.RUN
    assert result.attempts == 3
    assert len(calls) == 3


def test_handshake_gives_up_after_the_attempt_budget(probe_env, monkeypatch):
    """All attempts fail -> the decision is FAIL (not nested) with the budget used."""
    calls = []
    monkeypatch.setattr(
        live_server,
        "handshake",
        lambda *a, **k: (calls.append(1), (False, "TimeoutError: timed out"))[1],
    )

    result = live_server.probe(ENDPOINT, attempts=3, backoff=0)

    assert len(calls) == 3
    assert result.decision is Decision.FAIL_UNREACHABLE
    assert result.attempts == 3


# --- nested detection via /proc ancestry ---


def test_running_inside_server_detects_ancestor(monkeypatch):
    """An ancestor whose cmdline mentions bash_mcp.server means we are nested."""
    chain = {
        100: ("python3 -m bash_mcp.server --transport streamable-http", 99),
        99: ("uv run python -m bash_mcp.server --transport streamable-http", 1),
        1: ("/init", 0),
    }
    monkeypatch.setattr(live_server, "_current_pid", lambda: 100)
    monkeypatch.setattr(live_server, "_read_cmdline", lambda pid: chain[pid][0])
    monkeypatch.setattr(live_server, "_read_ppid", lambda pid: chain[pid][1])

    assert live_server.running_inside_server() is True


def test_running_inside_server_false_without_match(monkeypatch):
    """No ancestor mentions the server -> we are not nested."""
    chain = {
        100: ("pytest -q", 99),
        99: ("uv run pytest", 1),
        1: ("/init", 0),
    }
    monkeypatch.setattr(live_server, "_current_pid", lambda: 100)
    monkeypatch.setattr(live_server, "_read_cmdline", lambda pid: chain[pid][0])
    monkeypatch.setattr(live_server, "_read_ppid", lambda pid: chain[pid][1])

    assert live_server.running_inside_server() is False


def test_running_inside_server_false_when_proc_is_unreadable(monkeypatch):
    """Unreadable /proc -> assume not nested (the safe direction: fail, not skip)."""
    monkeypatch.setattr(live_server, "_current_pid", lambda: 100)
    monkeypatch.setattr(live_server, "_read_cmdline", lambda pid: None)
    monkeypatch.setattr(live_server, "_read_ppid", lambda pid: None)

    assert live_server.running_inside_server() is False


# --- SSE parsing ---


def test_handshake_parses_first_sse_data_line():
    """The reader extracts the payload of a `data:` line and ignores the rest."""
    assert live_server._extract_sse_payload("data: {\"jsonrpc\":\"2.0\"}") == '{"jsonrpc":"2.0"}'
    assert live_server._extract_sse_payload("event: message") is None
    assert live_server._extract_sse_payload("") is None
