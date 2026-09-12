"""Unit tests for audit log rotation."""

from __future__ import annotations

import json
import threading

import pytest

from bash_mcp import audit


@pytest.fixture
def temp_audit_dir(monkeypatch, tmp_path):
    """Redirect XDG_DATA_HOME to tmp_path and lower the rotation threshold for testing."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(audit, "_AUDIT_FILE", None)
    monkeypatch.setattr(audit, "MAX_AUDIT_BYTES", 1024)        # 1 KB
    monkeypatch.setattr(audit, "BACKUP_COUNT", 3)
    # v0.7 compat: explicitly disable gzip so test_audit_gzip.py state
    # (which may have set GZIP_THRESHOLD_BYTES > 0) does not affect us.
    monkeypatch.setattr(audit, "GZIP_THRESHOLD_BYTES", 0)
    target = tmp_path / "bash-mcp" / "audit.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def test_creates_file_on_first_write(temp_audit_dir):
    audit.log({"test": 1})
    assert temp_audit_dir.exists()


def test_rotates_when_size_exceeded(temp_audit_dir):
    """After enough writes to exceed 1 KB, the current file shrinks and a backup appears."""
    for i in range(20):
        audit.log({"i": i, "padding": "x" * 60})
    # Current file should be small (post-rotation)
    assert temp_audit_dir.stat().st_size < 1024
    # .1 backup should exist
    backup = temp_audit_dir.with_suffix(temp_audit_dir.suffix + ".1")
    assert backup.exists()


def test_keeps_only_n_backups(temp_audit_dir):
    """Force many rotations; never more than BACKUP_COUNT backups."""
    for i in range(500):
        audit.log({"i": i, "padding": "x" * 60})
    # BACKUP_COUNT = 3; numbered backups should not exceed 3
    parent = temp_audit_dir.parent
    backups = list(parent.glob("audit.jsonl.[0-9]"))
    assert len(backups) <= 3


def test_rotation_preserves_content(temp_audit_dir):
    """All written entries must be findable in audit*.jsonl files."""
    all_entries = []
    for i in range(100):
        audit.log({"i": i})
        all_entries.append(i)
    files = [temp_audit_dir] + sorted(temp_audit_dir.parent.glob("audit.jsonl.[0-9]"))
    found = []
    for f in files:
        for line in f.read_text().splitlines():
            if line.strip():
                found.append(json.loads(line).get("i"))
    assert sorted(found) == all_entries


def test_concurrent_log_does_not_corrupt_json(temp_audit_dir):
    """Concurrent log() calls from multiple threads should not corrupt JSONL."""
    def writer(start: int) -> None:
        for i in range(50):
            audit.log({"writer": start, "i": i})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every line in every audit file must be valid JSON
    files = [temp_audit_dir] + sorted(temp_audit_dir.parent.glob("audit.jsonl.[0-9]"))
    total_lines = 0
    for f in files:
        for line in f.read_text().splitlines():
            if line.strip():
                json.loads(line)  # raises if corrupted
                total_lines += 1
    # 4 writers * 50 entries = 200 entries
    assert total_lines == 200


def test_backup_paths_returns_existing_only(temp_audit_dir):
    """backup_paths() only lists backups that actually exist."""
    assert audit.backup_paths() == []
    # Force one rotation
    for i in range(20):
        audit.log({"i": i, "padding": "x" * 60})
    paths = audit.backup_paths()
    assert len(paths) >= 1
    assert all(p.exists() for p in paths)
