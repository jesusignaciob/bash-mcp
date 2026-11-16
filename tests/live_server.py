"""Probe for the live bash-mcp server used by the end-to-end suite.

Why this module exists
----------------------
The e2e suite used to decide whether to run with a module-level
`pytest.mark.skipif(not _server_reachable(), ...)` evaluated at import
time. Any probe failure - including "the server is busy" - silently
skipped all 24 e2e tests, and the suite still exited 0.

That was not a rare edge case. The bash-mcp server serializes its work:
while it executes a tool call it does not answer any other MCP request
(measured: `initialize` and `tools/list` both time out during a `sleep 12`
tool call, and answer in 0.01 s once it finishes). Since the natural way
to run the suite in this repo is *through* bash-mcp
(`bash_run_command` -> `uv run pytest`, which is what the skill asks the
agent to do), the pytest process asked the very server that was serving
it, the probe hung, and a third of the tests disappeared from the run
without a word.

The decision matrix
-------------------
    port closed                    -> SKIP_NO_SERVER    (honest skip)
    port open + handshake OK       -> RUN
    port open + handshake fails
       + nested inside the server  -> SKIP_NESTED       (honest, explicit skip)
       + not nested                -> FAIL_UNREACHABLE  (red, never a silent green)

The "nested" case is detected by walking the /proc ancestry looking for
the server's command line (`bash_mcp.server`). When we are a descendant of
the server, the server cannot answer us while it is serving us - that is
a limitation of running the suite in-process, not a broken server, so it
is an explicit skip. When the server listens but does not answer and we
are NOT inside it, something is genuinely wrong (wedged or busy with
another client): the suite fails instead of quietly dropping the tests.

Reading only the first SSE `data:` line keeps the handshake cheap and
independent of how long the server holds the stream open.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.request
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


ENDPOINT = "http://127.0.0.1:54321/mcp/"

# Marker found in the command line of the running server process.
SERVER_MARKER = "bash_mcp.server"

# How far up the process tree we are willing to look.
MAX_ANCESTRY_DEPTH = 24

# Default handshake budget: a momentarily busy server may answer later.
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF = 1.0


class Decision(str, Enum):
    """What the e2e suite should do with the live server."""

    RUN = "run"
    SKIP_NO_SERVER = "skip_no_server"
    SKIP_NESTED = "skip_nested"
    FAIL_UNREACHABLE = "fail_unreachable"


@dataclass
class ProbeResult:
    """Outcome of a probe, with a reason fit to show the user."""

    decision: Decision
    reason: str
    endpoint: str = ENDPOINT
    attempts: int = 0
    detail: str = ""


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when something accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _extract_sse_payload(line: str) -> str | None:
    """Return the JSON payload of an SSE `data:` line, or None."""
    text = line.strip()
    if text.startswith("data: "):
        return text[6:]
    return None


def handshake(endpoint: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Perform an MCP `initialize` against the server.

    Returns `(ok, detail)`. Never raises: a transport error is a failed
    handshake, not an exception for the caller to handle.
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "bash-mcp-probe", "version": "0.1"},
        },
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            session_id = response.headers.get("mcp-session-id")
            for raw in response:
                payload = _extract_sse_payload(raw.decode("utf-8", "replace"))
                if payload is not None:
                    if not session_id:
                        return False, "response carried no Mcp-Session-Id header"
                    return True, f"handshake ok (session {session_id[:8]})"
            return False, "no 'data:' line in the SSE response"
    except Exception as exc:  # noqa: BLE001 — any transport error is a failed probe
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def _current_pid() -> int:
    """PID to start the ancestry walk from (indirection keeps it testable)."""
    return os.getpid()


def _read_cmdline(pid: int) -> str | None:
    """Command line of `pid`, or None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return None


def _read_ppid(pid: int) -> int | None:
    """Parent PID of `pid`, or None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        return int(fields[1])
    except (OSError, IndexError, ValueError):
        return None


def running_inside_server() -> bool:
    """True when one of our ancestors is the bash-mcp server.

    When the suite runs through bash-mcp, the server is busy serving us
    and cannot answer the probe - that is the "nested" case. If /proc
    cannot be read the answer is False, which is the safe direction: the
    probe then fails loudly instead of skipping silently.
    """
    pid = _current_pid()
    for _ in range(MAX_ANCESTRY_DEPTH):
        cmdline = _read_cmdline(pid)
        if cmdline and SERVER_MARKER in cmdline:
            return True
        parent = _read_ppid(pid)
        if parent is None or parent <= 1:
            return False
        pid = parent
    return False


def probe(
    endpoint: str = ENDPOINT,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
) -> ProbeResult:
    """Decide what the e2e suite should do with the live server."""
    parsed = urlparse(endpoint)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 54321

    if not port_open(host, port):
        return ProbeResult(
            Decision.SKIP_NO_SERVER,
            f"no bash-mcp server listening on {host}:{port} ({endpoint})",
            endpoint=endpoint,
        )

    detail = ""
    for attempt in range(1, max(1, attempts) + 1):
        ok, detail = handshake(endpoint)
        if ok:
            return ProbeResult(
                Decision.RUN,
                f"bash-mcp server reachable at {endpoint}",
                endpoint=endpoint,
                attempts=attempt,
                detail=detail,
            )
        if attempt < attempts:
            time.sleep(backoff)

    if running_inside_server():
        return ProbeResult(
            Decision.SKIP_NESTED,
            (
                "running inside the bash-mcp server: it cannot answer the probe "
                "while serving this run - run the e2e tests from an external shell"
            ),
            endpoint=endpoint,
            attempts=attempts,
            detail=detail,
        )

    return ProbeResult(
        Decision.FAIL_UNREACHABLE,
        (
            f"a process is listening on {host}:{port} but the MCP handshake "
            f"failed after {attempts} attempt(s); the server may be wedged or "
            f"busy with another request"
        ),
        endpoint=endpoint,
        attempts=attempts,
        detail=detail,
    )
