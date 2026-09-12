"""Append-only JSONL audit log with size-based rotation.

Each entry is a single JSON object written in one write() call. On Linux,
writes smaller than PIPE_BUF (4KB) are atomic, so concurrent appenders
won't corrupt the file.

When the file exceeds MAX_AUDIT_BYTES, it is rotated to audit.jsonl.1,
the previous .1 becomes .2, etc., and the oldest (.BACKUP_COUNT) is deleted.
Rotation is lazy (happens on the next write after the size threshold is
crossed) and guarded by a single lock so rotation + append are atomic.

Location: $XDG_DATA_HOME/bash-mcp/audit.jsonl  (default ~/.local/share/bash-mcp/audit.jsonl)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path


# Defaults overridable via env. Module-level so tests can monkeypatch.
MAX_AUDIT_BYTES: int = int(os.environ.get("BASH_MCP_AUDIT_MAX_BYTES", str(25 * 1024 * 1024)))
BACKUP_COUNT: int = int(os.environ.get("BASH_MCP_AUDIT_BACKUP_COUNT", "5"))

# v0.7: gzip-on-rotation. When a rotated backup exceeds this size, it
# is gzipped to .jsonl.N.gz and the plaintext is deleted. Default 0 =
# disabled (preserves the v0.3 plaintext-only rotation behavior).
# Decompression is NOT implemented in v0.7; use `zcat audit.jsonl.N.gz`
# to inspect archived backups.
DEFAULT_GZIP_THRESHOLD_BYTES: int = 0
GZIP_THRESHOLD_BYTES: int = int(
    os.environ.get("BASH_MCP_AUDIT_GZIP_THRESHOLD_BYTES", str(DEFAULT_GZIP_THRESHOLD_BYTES))
)

# Thread-safety: FastMCP runs tools in a thread pool; multiple threads may
# call log() concurrently. One lock guards both rotation and append.
_AUDIT_LOCK = threading.Lock()


def _audit_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "bash-mcp" / "audit.jsonl"


_AUDIT_FILE: Path | None = None


def _ensure_path() -> Path:
    """Resolve the audit log path, creating the parent dir on first call."""
    global _AUDIT_FILE
    if _AUDIT_FILE is None:
        p = _audit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        _AUDIT_FILE = p
    return _AUDIT_FILE


def audit_path() -> Path:
    """Public accessor for the canonical audit log path."""
    return _ensure_path()


def backup_paths() -> list[Path]:
    """Return paths of existing backups (.jsonl.N or .jsonl.N.gz).

    v0.7: prefers .gz over plaintext when both exist (gzip supersedes).
    """
    p = _ensure_path()
    out: list[Path] = []
    for n in range(1, BACKUP_COUNT + 1):
        gz = p.with_suffix(p.suffix + f".{n}.gz")
        pt = p.with_suffix(p.suffix + f".{n}")
        if gz.exists():
            out.append(gz)
        elif pt.exists():
            out.append(pt)
    return out


def _rotate_if_needed(path: Path) -> None:
    """Rotate the audit log if it exceeds MAX_AUDIT_BYTES.

    Naming: audit.jsonl -> audit.jsonl.1, .1 -> .2, ..., .N -> deleted.
    Must be called with _AUDIT_LOCK held.

    If the rename fails (e.g., another writer has the file open on Windows),
    the OSError propagates to log() which catches it and prints to stderr.
    The next log() call will retry rotation.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < MAX_AUDIT_BYTES:
        return

    # Drop oldest backup (audit.jsonl.BACKUP_COUNT)
    oldest = path.with_suffix(path.suffix + f".{BACKUP_COUNT}")
    if oldest.exists():
        oldest.unlink()
    # Shift backups: .N-1 -> .N, ..., .1 -> .2
    for n in range(BACKUP_COUNT - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{n}")
        if src.exists():
            dst = path.with_suffix(path.suffix + f".{n + 1}")
            src.rename(dst)
    # Current -> .1
    backup1 = path.with_suffix(path.suffix + ".1")
    path.rename(backup1)

    # v0.7 gzip pass — opt-in (only runs when GZIP_THRESHOLD_BYTES > 0).
    # Walks .1..BACKUP_COUNT and gzips any backup whose size exceeds the
    # threshold. Plaintext is deleted after a successful gzip; on failure
    # the plaintext is kept (fail-soft).
    if GZIP_THRESHOLD_BYTES > 0:
        _gzip_pass(path)


def _gzip_pass(path: Path) -> None:
    """Gzip any plaintext backup whose size exceeds GZIP_THRESHOLD_BYTES.

    Called from _rotate_if_needed with _AUDIT_LOCK held. Uses chunked
    copy so backups up to tens of MB don't load fully into memory.
    Failures are isolated per backup: a failed gzip keeps the plaintext
    and prints a warning to stderr; the rest of the sweep continues.
    """
    if GZIP_THRESHOLD_BYTES <= 0:
        return
    import gzip  # lazy import — only paid by users who enable gzip

    for n in range(1, BACKUP_COUNT + 1):
        backup = path.with_suffix(path.suffix + f".{n}")
        if not backup.exists():
            continue
        try:
            backup_size = backup.stat().st_size
        except FileNotFoundError:
            continue
        if backup_size < GZIP_THRESHOLD_BYTES:
            continue

        gz_path = backup.with_suffix(backup.suffix + ".gz")
        try:
            with backup.open("rb") as f_in, \
                 gzip.open(gz_path, "wb", compresslevel=6) as f_out:
                while True:
                    chunk = f_in.read(64 * 1024)
                    if not chunk:
                        break
                    f_out.write(chunk)
            backup.unlink()
        except OSError as e:
            print(
                f"[bash-mcp audit] WARN: gzip failed for {backup}: {e}",
                file=sys.stderr,
            )
            if gz_path.exists():
                try:
                    gz_path.unlink()
                except OSError:
                    pass


def new_audit_id() -> str:
    return f"{time.strftime('%Y-%m-%dT%H:%M:%S')}-{uuid.uuid4().hex[:8]}"


def log(entry: dict) -> None:
    """Append a single audit entry, rotating first if needed.

    Fail-soft: any I/O error goes to stderr (never blocks the tool call).
    Thread-safe: one global lock guards rotation + append.
    """
    try:
        with _AUDIT_LOCK:
            path = _ensure_path()
            _rotate_if_needed(path)
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
