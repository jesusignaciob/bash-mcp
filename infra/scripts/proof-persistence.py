#!/usr/bin/env python3
"""End-to-end proof of session persistence (v0.9) against the live server.

Two steps, because the point is what survives a **restart**:

    # 1. create a session with a distinctive cwd + env
    uv run python infra/scripts/proof-persistence.py create

    # 2. restart the server, then check the session came back
    uv run python infra/scripts/proof-persistence.py check <session_id>

Exit code 0 = the session survived with its cwd and env intact.
"""

from __future__ import annotations

import json
import sys
import urllib.request


ENDPOINT = "http://127.0.0.1:54321/mcp/"
MARKER = "v09-persistence-proof"


def http_post(body, headers=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.headers.get("mcp-session-id"), r.read().decode()


def parse_data(body: str):
    """Extract the JSON payload from an SSE `data:` line."""
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None


def call_tool(sid, name, arguments=None):
    _, body = http_post(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        headers={"Mcp-Session-Id": sid},
    )
    payload = parse_data(body)
    if "error" in payload:
        return {"_transport_error": payload["error"]}
    return json.loads(payload["result"]["content"][0]["text"])


def connect() -> str:
    sid, _ = http_post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "v09-proof", "version": "0.1"},
            },
        }
    )
    http_post(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Mcp-Session-Id": sid},
    )
    return sid


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    mode = sys.argv[1]
    sid = connect()

    if mode == "create":
        created = call_tool(
            sid, "bash_mcp_session_create", {"name": MARKER, "cwd": "/tmp"}
        )
        session_id = created.get("session_id")
        call_tool(
            sid,
            "bash_mcp_session_run",
            {
                "session_id": session_id,
                "command": "cd /home/jbecerra && export V09PROOF=alive",
            },
        )
        listed = call_tool(sid, "bash_mcp_session_list")
        found = next(
            (s for s in listed.get("sessions", []) if s["session_id"] == session_id),
            None,
        )
        status = call_tool(sid, "bash_mcp_status")
        print("SESSION_ID=" + str(session_id))
        print("BEFORE_RESTART=" + json.dumps(found, sort_keys=True))
        print(
            "PERSISTED="
            + json.dumps(
                status.get("sessions", {}).get("persistence", {}), sort_keys=True
            )
        )
        return 0

    if mode == "check":
        target = sys.argv[2]
        listed = call_tool(sid, "bash_mcp_session_list")
        found = next(
            (s for s in listed.get("sessions", []) if s["session_id"] == target), None
        )
        status = call_tool(sid, "bash_mcp_status")
        print("AFTER_RESTART=" + json.dumps(found, sort_keys=True))
        print(
            "PERSISTED="
            + json.dumps(
                status.get("sessions", {}).get("persistence", {}), sort_keys=True
            )
        )
        if found is None:
            print("RESULT=FAIL (the session did NOT survive the restart)")
            return 1
        if "V09PROOF" not in found.get("env_keys", []):
            print("RESULT=FAIL (the session came back but lost its env)")
            return 1
        if found.get("cwd") != "/home/jbecerra":
            print("RESULT=FAIL (the session came back but lost its cwd)")
            return 1
        print("RESULT=PASS (session restored with cwd + env intact)")
        return 0

    print("usage: proof-persistence.py create | check <session_id>")
    return 2


if __name__ == "__main__":
    sys.exit(main())
