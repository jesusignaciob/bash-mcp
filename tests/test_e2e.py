"""End-to-end test against a running bash-mcp server.

Hits the live streamable-http endpoint at http://127.0.0.1:54321/mcp/.
The server must be running (via systemd or `uv run python -m bash_mcp.server`).
Skipped automatically if the server is not reachable.

Run:
    uv run pytest tests/test_e2e.py -v
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest

ENDPOINT = "http://127.0.0.1:54321/mcp/"


def _server_reachable() -> bool:
    try:
        r = httpx.post(
            ENDPOINT,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "test-e2e", "version": "0.1"}},
            },
            timeout=3.0,
        )
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason="bash-mcp server not reachable at " + ENDPOINT,
)


def _mcp_session() -> tuple[str, str]:
    """Initialize a session, return (session_id, message_endpoint)."""
    with httpx.Client(timeout=5.0) as c:
        r = c.post(
            ENDPOINT,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "test-e2e", "version": "0.1"}},
            },
        )
        assert r.status_code == 200
        sid = r.headers.get("mcp-session-id")
        assert sid, f"no session id in headers: {dict(r.headers)}"

        c.post(
            ENDPOINT,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "Mcp-Session-Id": sid},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        return sid, ENDPOINT


def _call_tool(sid: str, name: str, arguments: dict | None = None) -> dict:
    """Call a tool and return the parsed result content (first text block)."""
    with httpx.Client(timeout=10.0) as c:
        r = c.post(
            ENDPOINT,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "Mcp-Session-Id": sid},
            json={
                "jsonrpc": "2.0", "id": 99,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
        )
        assert r.status_code == 200, f"{r.status_code}: {r.text}"

    m = re.search(r"^data: (.+)$", r.text, re.MULTILINE)
    assert m, f"no data line in response: {r.text!r}"
    payload = json.loads(m.group(1))
    content = payload.get("result", {}).get("content", [])
    assert content, f"no content in payload: {payload}"
    text = content[0]["text"]
    return json.loads(text)


def test_tools_list(sid: str) -> None:
    """All 6 tools (5 bash-mcp + echo) should be listed."""
    with httpx.Client(timeout=5.0) as c:
        r = c.post(
            ENDPOINT,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "Mcp-Session-Id": sid},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert r.status_code == 200
        m = re.search(r"^data: (.+)$", r.text, re.MULTILINE)
        assert m
        payload = json.loads(m.group(1))
    names = sorted(t["name"] for t in payload["result"]["tools"])
    assert names == ["bash_check_env", "bash_list_binaries", "bash_mcp_status",
                     "bash_run_command", "bash_which", "echo"]


def test_run_command_echo(sid: str) -> None:
    """echo hello returns exit_code 0 and stdout."""
    out = _call_tool(sid, "bash_run_command", {"command": "echo hello"})
    assert out["exit_code"] == 0
    assert "hello" in out["stdout"]
    assert out["timed_out"] is False
    assert "audit_id" in out
    assert out["classification"]["class"] == "safe"


def test_run_command_ls_nonexistent(sid: str) -> None:
    """ls /nonexistent returns exit_code 2."""
    out = _call_tool(sid, "bash_run_command", {"command": "ls /nonexistent/path/zzz"})
    assert out["exit_code"] == 2
    assert "No such file" in out["stderr"]


def test_run_command_rejects_hard(sid: str) -> None:
    """rm -rf /etc must be REJECTED with hint."""
    out = _call_tool(sid, "bash_run_command", {"command": "rm -rf /etc"})
    assert "error" in out
    err = out["error"]
    assert err["code"] == "FORBIDDEN_COMMAND"
    assert "hint" in err
    assert "documentation" in err
    assert "dangerous=true" in err["hint"]  # mentions no-bypass
    assert out["classification"]["class"] == "reject"


def test_run_command_dangerous_requires_override(sid: str) -> None:
    """sudo without dangerous=true must be DENIED with override hint."""
    out = _call_tool(sid, "bash_run_command", {"command": "sudo whoami"})
    assert "error" in out
    err = out["error"]
    assert err["code"] == "DANGEROUS_COMMAND_REQUIRES_OVERRIDE"
    assert "dangerous=true" in err["hint"]


def test_run_command_timeout(sid: str) -> None:
    """sleep 10 with timeout 800ms must report timed_out=true."""
    out = _call_tool(sid, "bash_run_command",
                     {"command": "sleep 10", "timeout_ms": 800})
    assert out["timed_out"] is True
    assert out["exit_code"] == -9
    assert out["duration_ms"] >= 700  # some slack


def test_check_env(sid: str) -> None:
    """check_env returns OS info."""
    out = _call_tool(sid, "bash_check_env")
    assert "Linux" in out["system"]
    assert "python_version" in out
    assert "path_entries" in out
    assert isinstance(out["path_entries"], list)


def test_list_binaries(sid: str) -> None:
    """list_binaries returns >=10 entries, sorted by exists."""
    out = _call_tool(sid, "bash_list_binaries")
    assert isinstance(out, list)
    assert len(out) >= 10
    rg = next((b for b in out if b["name"] == "rg"), None)
    assert rg is not None
    assert rg["exists"] is True


def test_which(sid: str) -> None:
    """which resolves rg, reports missing for nonsense."""
    rg = _call_tool(sid, "bash_which", {"name": "rg"})
    assert rg["exists"] is True
    assert rg["path"]

    missing = _call_tool(sid, "bash_which", {"name": f"definitely-not-real-{uuid.uuid4().hex[:6]}"})
    assert missing["exists"] is False
    assert missing["path"] is None


def test_status(sid: str) -> None:
    """bash_mcp_status returns service health + audit stats."""
    out = _call_tool(sid, "bash_mcp_status")
    assert out["service"] == "active"
    assert isinstance(out["version"], str)
    assert out["uptime_seconds"] >= 0
    assert isinstance(out["start_time"], str)
    # Audit stats (v0.2 fields)
    assert "audit" in out
    assert isinstance(out["audit"]["entries"], int)
    assert out["audit"]["entries"] >= 0
    assert isinstance(out["audit"]["size_bytes"], int)
    assert out["audit"]["size_bytes"] >= 0
    # Audit stats (v0.3 fields — rotation)
    assert "max_bytes" in out["audit"]
    assert isinstance(out["audit"]["max_bytes"], int)
    assert out["audit"]["max_bytes"] > 0
    assert "backup_count" in out["audit"]
    assert isinstance(out["audit"]["backup_count"], int)
    assert out["audit"]["backup_count"] >= 1
    assert "backups_present" in out["audit"]
    assert isinstance(out["audit"]["backups_present"], list)
    # Concurrency (v0.3 fields)
    assert "concurrency" in out
    assert out["concurrency"]["max_concurrent"] >= 1
    assert isinstance(out["concurrency"]["active"], int)
    assert out["concurrency"]["active"] >= 0
    # Process info
    assert isinstance(out["process"]["pid"], int)
    # Tools list (self-reference included)
    assert "bash_run_command" in out["tools"]
    assert "bash_check_env" in out["tools"]
    assert "bash_list_binaries" in out["tools"]
    assert "bash_which" in out["tools"]
    assert "bash_mcp_status" in out["tools"]
    assert "echo" in out["tools"]


def test_invalid_cwd_returns_hint(sid: str) -> None:
    """INVALID_CWD error must include hint + documentation fields."""
    out = _call_tool(sid, "bash_run_command", {"command": "echo x", "cwd": "/etc"})
    assert "error" in out
    err = out["error"]
    assert err["code"] == "INVALID_CWD"
    assert "hint" in err
    assert "documentation" in err
    assert "allowed roots" in err["hint"].lower()


@pytest.fixture
def sid() -> str:
    s, _ = _mcp_session()
    return s
