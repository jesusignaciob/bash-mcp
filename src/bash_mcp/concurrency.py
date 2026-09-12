"""Concurrency limiter for subprocess execution.

FastMCP 2.7.0 runs sync tools in a thread pool (anyio.to_thread), so we use
a threading.BoundedSemaphore to bound the number of concurrent subprocess
spawns. The semaphore is process-global (module-level).

Configured via BASH_MCP_MAX_CONCURRENT env var (default 8).

Usage:
    from bash_mcp.concurrency import slot

    with slot():
        subprocess.run([...])
"""

from __future__ import annotations

import os
import threading

MAX_CONCURRENT: int = int(os.environ.get("BASH_MCP_MAX_CONCURRENT", "8"))

# BoundedSemaphore detects bugs: releases > acquires raise ValueError.
_LIMIT = threading.BoundedSemaphore(MAX_CONCURRENT)

# Best-effort counter for status reporting. Guarded by _ACTIVE_LOCK.
_ACTIVE = 0
_ACTIVE_LOCK = threading.Lock()


class slot:
    """Context manager: `with concurrency.slot(): ...` blocks if at capacity."""

    def __enter__(self) -> "slot":
        global _ACTIVE
        _LIMIT.acquire()
        with _ACTIVE_LOCK:
            _ACTIVE += 1
        return self

    def __exit__(self, *exc) -> bool:
        global _ACTIVE
        with _ACTIVE_LOCK:
            _ACTIVE -= 1
        _LIMIT.release()
        return False


def active() -> int:
    """Approximate number of currently-held slots. Read-only."""
    with _ACTIVE_LOCK:
        return _ACTIVE
