"""Append-only JSONL audit log.

Each entry is a single JSON object written in one write() call. On Linux,
writes smaller than PIPE_BUF (4KB) are atomic, so concurrent appenders
won't corrupt the file. We never read from this log inside the server —
it's purely observability.

Location: $XDG_DATA_HOME/bash-mcp/audit.jsonl  (default ~/.local/share/bash-mcp/audit.jsonl)
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path


def _audit_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "bash-mcp" / "audit.jsonl"


_AUDIT_FILE: Path | None = None


def _ensure_open() -> Path:
    global _AUDIT_FILE
    if _AUDIT_FILE is not None:
        return _AUDIT_FILE
    p = _audit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _AUDIT_FILE = p
    return p


def new_audit_id() -> str:
    return f"{time.strftime('%Y-%m-%dT%H:%M:%S')}-{uuid.uuid4().hex[:8]}"


def log(entry: dict) -> None:
    """Append a single audit entry. Fail-soft: any I/O error goes to stderr."""
    try:
        path = _ensure_open()
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[bash-mcp audit] WARN: failed to write audit log: {e}", file=sys.stderr)


def log_command(
    *,
    tool: str,
    args: dict,
    classification: dict,
    exit_code: int,
    duration_ms: int,
    stdout_bytes: int,
    stderr_bytes: int,
    timed_out: bool,
    truncated: bool,
    audit_id: str | None = None,
) -> str:
    """Convenience wrapper for command-execution audit entries."""
    aid = audit_id or new_audit_id()
    log({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "audit_id": aid,
        "tool": tool,
        "args": args,
        "classification": classification,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "timed_out": timed_out,
        "truncated": truncated,
    })
    return aid
