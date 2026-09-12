"""Unit tests for the concurrency limiter."""

from __future__ import annotations

import threading
import time

import pytest

from bash_mcp import concurrency
from bash_mcp.executor import run as exec_run


def _reset_limit(n: int) -> None:
    """Reset the module-level semaphore + counter to a known size.

    Tests mutate global state; this helper keeps each test isolated.
    """
    concurrency._LIMIT = threading.BoundedSemaphore(n)
    concurrency._ACTIVE = 0
    concurrency._ACTIVE_LOCK = threading.Lock()


def test_slot_blocks_when_full():
    """With limit=2, only 2 workers should be in-flight simultaneously."""
    _reset_limit(2)
    entered: list[int] = []
    lock = threading.Lock()

    barrier = threading.Event()

    def worker(i: int) -> None:
        with concurrency.slot():
            with lock:
                entered.append(i)
            # Sleep briefly so we can observe concurrency
            barrier.wait(timeout=0.5)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    # Give the first 2 a moment to enter
    time.sleep(0.05)
    assert len(entered) <= 2, f"expected ≤ 2 in-flight, got {len(entered)}"
    barrier.set()
    for t in threads:
        t.join(timeout=2.0)
    assert len(entered) == 5


def test_active_count_tracks():
    _reset_limit(4)
    assert concurrency.active() == 0
    with concurrency.slot():
        assert concurrency.active() == 1
        with concurrency.slot():
            assert concurrency.active() == 2
        assert concurrency.active() == 1
    assert concurrency.active() == 0


def test_executor_respects_limit():
    """Real subprocess calls should be bounded by the semaphore."""
    _reset_limit(3)
    started_at: list[float] = []
    finished_at: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        with lock:
            started_at.append(time.monotonic())
        exec_run("sleep 0.1")
        with lock:
            finished_at.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    # 6 calls, limit=3, each 100ms → total ~200ms minimum
    elapsed = max(finished_at) - min(started_at)
    assert 0.18 < elapsed < 1.0, f"expected ~200ms (limit=3, 6×100ms), got {elapsed*1000:.0f}ms"


def test_bounded_semaphore_detects_bug():
    """Releasing without acquire raises ValueError (catches bugs)."""
    _reset_limit(1)
    with pytest.raises(ValueError):
        concurrency._LIMIT.release()


def test_max_concurrent_default_is_eight():
    """Default MAX_CONCURRENT is 8 (per design decision)."""
    assert concurrency.MAX_CONCURRENT == 8
