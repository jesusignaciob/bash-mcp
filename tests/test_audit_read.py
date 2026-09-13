"""Unit tests for audit log read-back (v0.7.1).

Reuses the temp_audit_dir fixture from test_audit_rotation.py, with
gzip opt-in via monkeypatch.setattr(audit, "GZIP_THRESHOLD_BYTES", N).
"""

from __future__ import annotations

import gzip

import pytest

from bash_mcp import audit


@pytest.fixture
def temp_audit_dir(monkeypatch, tmp_path):
    """Redirect XDG_DATA_HOME + lower rotation threshold; opt-in gzip via per-test."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(audit, "_AUDIT_FILE", None)
    monkeypatch.setattr(audit, "MAX_AUDIT_BYTES", 1024)        # 1 KB
    monkeypatch.setattr(audit, "BACKUP_COUNT", 3)
    # Default: gzip disabled. Individual tests opt in.
    monkeypatch.setattr(audit, "GZIP_THRESHOLD_BYTES", 0)
    target = tmp_path / "bash-mcp" / "audit.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


# --- path resolution ---


def test_read_current_returns_path_to_audit_jsonl(temp_audit_dir):
    """read_backup_path(0) returns the current audit log (after a write)."""
    audit.log({"x": 1})  # create the file
    path = audit.read_backup_path(0)
    assert path is not None
    assert path.name == "audit.jsonl"


def test_read_backup_path_prefers_gz_over_plaintext(temp_audit_dir, monkeypatch):
    """When both .1 and .1.gz exist, return .1.gz (v0.7 gzip supersedes)."""
    monkeypatch.setattr(audit, "GZIP_THRESHOLD_BYTES", 1)
    for i in range(40):
        audit.log({"i": i, "pad": "x" * 200})
    parent = temp_audit_dir.parent
    gz = parent / "audit.jsonl.1.gz"
    pt = parent / "audit.jsonl.1"
    if gz.exists() and pt.exists():
        path = audit.read_backup_path(1)
        assert path.name.endswith(".gz")


def test_read_backup_path_returns_none_for_missing(temp_audit_dir):
    assert audit.read_backup_path(99) is None
    assert audit.read_backup_path(0) is None  # no log written yet


# --- read behavior ---


def test_read_current_backup_returns_entries(temp_audit_dir):
    audit.log({"a": 1})
    audit.log({"b": 2})
    entries = audit.read_backup(0)
    assert len(entries) == 2
    assert entries[0]["a"] == 1
    assert entries[1]["b"] == 2


def test_read_plaintext_backup_after_rotation(temp_audit_dir):
    """Write enough to force rotation; read_backup(1) returns older entries."""
    for i in range(30):
        audit.log({"i": i, "pad": "x" * 60})
    # Force at least one rotation. Backup 1 should exist.
    path = audit.read_backup_path(1)
    assert path is not None
    entries = audit.read_backup(1)
    assert len(entries) > 0
    assert all("i" in e for e in entries)


def test_read_gzipped_backup_decompresses(temp_audit_dir, monkeypatch):
    """With GZIP_THRESHOLD_BYTES=1, backups are gzipped; read_backup decompresses."""
    monkeypatch.setattr(audit, "GZIP_THRESHOLD_BYTES", 1)
    for i in range(40):
        audit.log({"i": i, "pad": "x" * 200})

    path = audit.read_backup_path(1)
    assert path is not None
    assert path.name.endswith(".gz")

    entries = audit.read_backup(1)
    assert len(entries) > 0
    # Verify entries are valid JSON-decoded dicts.
    assert all(isinstance(e, dict) for e in entries)


def test_read_gzipped_backup_yields_consistent_entries(temp_audit_dir, monkeypatch):
    """Reading the same gz backup twice returns the same entries (idempotent).

    Stronger than a cross-compare with plaintext (which is flaky due to
    rotation timing differences when re-running with different settings).
    """
    monkeypatch.setattr(audit, "GZIP_THRESHOLD_BYTES", 1)
    for i in range(50):
        audit.log({"kind": "gztest", "i": i, "pad": "x" * 200})

    path = audit.read_backup_path(1)
    assert path is not None
    assert path.name.endswith(".gz")

    first = audit.read_backup(1)
    second = audit.read_backup(1)
    assert first == second
    assert len(first) > 0
    assert all(isinstance(e, dict) and e.get("kind") == "gztest" for e in first)


def test_read_nonexistent_backup_returns_empty_list(temp_audit_dir):
    """No exception; just an empty list."""
    entries = audit.read_backup(99)
    assert entries == []


# --- filtering ---


def test_read_with_tool_filter(temp_audit_dir):
    """tool_filter restricts to entries with matching tool field."""
    audit.log({"tool": "bash_run_command", "i": 1})
    audit.log({"tool": "bash_run_command", "i": 2})
    audit.log({"tool": "bash_mcp_classify", "i": 3})

    run_entries = audit.read_backup(0, tool_filter="bash_run_command")
    assert len(run_entries) == 2
    assert all(e["tool"] == "bash_run_command" for e in run_entries)

    classify_entries = audit.read_backup(0, tool_filter="bash_mcp_classify")
    assert len(classify_entries) == 1
    assert classify_entries[0]["i"] == 3


def test_read_max_entries_caps_result(temp_audit_dir):
    """Hard cap stops reading after max_entries matching entries."""
    for i in range(50):
        audit.log({"i": i})
    entries = audit.read_backup(0, max_entries=10)
    assert len(entries) == 10


# --- fail-soft ---


def test_read_handles_malformed_line_gracefully(temp_audit_dir, capsys):
    """Malformed lines are skipped with a stderr warning, not raised."""
    audit.log({"valid": 1})
    # Append a garbage line.
    with temp_audit_dir.open("a", encoding="utf-8") as f:
        f.write("this is not json\n")
    audit.log({"valid": 2})

    entries = audit.read_backup(0)
    assert len(entries) == 2
    assert {e["valid"] for e in entries} == {1, 2}
    captured = capsys.readouterr()
    assert "malformed line" in captured.err


def test_read_handles_corrupted_gz_file(temp_audit_dir, monkeypatch, capsys):
    """Corrupted .gz file: reader returns [] with stderr warning, no exception."""
    monkeypatch.setattr(audit, "GZIP_THRESHOLD_BYTES", 1)
    # Write garbage to a .gz path (no real gzip stream).
    gz_path = temp_audit_dir.parent / "audit.jsonl.1.gz"
    gz_path.write_bytes(b"this is not a valid gzip stream\n")

    entries = audit.read_backup(1)
    assert entries == []
    captured = capsys.readouterr()
    assert "could not read" in captured.err or "WARN" in captured.err
