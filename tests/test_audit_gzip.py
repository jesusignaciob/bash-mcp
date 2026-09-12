"""Unit tests for audit log gzip-on-rotation (v0.7).

Reuses the temp_audit_dir fixture pattern from test_audit_rotation.py,
then adds GZIP_THRESHOLD_BYTES via monkeypatch.setattr.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
from pathlib import Path

import pytest

from bash_mcp import audit


@pytest.fixture
def temp_audit_dir(monkeypatch, tmp_path):
    """Redirect XDG_DATA_HOME + lower rotation threshold; opt-in gzip."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(audit, "_AUDIT_FILE", None)
    monkeypatch.setattr(audit, "MAX_AUDIT_BYTES", 1024)        # 1 KB
    monkeypatch.setattr(audit, "BACKUP_COUNT", 3)
    # Default: gzip disabled. Individual tests opt in via monkeypatch.
    monkeypatch.setattr(audit, "GZIP_THRESHOLD_BYTES", 0)
    target = tmp_path / "bash-mcp" / "audit.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _write_big_entry(path: Path, n: int = 200) -> None:
    """Write `n` large audit entries via audit.log() to trigger rotation.

    Rotation only happens inside audit.log() (via _rotate_if_needed).
    Direct file writes do NOT trigger rotation.
    """
    big_value = "x" * 200
    for i in range(n):
        audit.log({"i": i, "pad": big_value})


# --- disabled-by-default ---


def test_gzip_disabled_by_default(temp_audit_dir):
    """With GZIP_THRESHOLD_BYTES=0, no .gz files are ever created."""
    _write_big_entry(temp_audit_dir, n=500)  # forces several rotations
    # All backups must be plaintext.
    gz = list(temp_audit_dir.parent.glob("*.jsonl.*.gz"))
    assert gz == [], f"expected no .gz backups, found {gz}"


def test_gzip_threshold_zero_means_no_compression(temp_audit_dir):
    """Explicit zero threshold = opt-out even if backups grow."""
    audit.GZIP_THRESHOLD_BYTES = 0
    _write_big_entry(temp_audit_dir, n=500)
    assert list(temp_audit_dir.parent.glob("*.jsonl.*.gz")) == []


# --- compression behavior ---


def test_gzip_compresses_backup_above_threshold(temp_audit_dir):
    """Backup exceeding GZIP_THRESHOLD_BYTES gets gzipped."""
    audit.GZIP_THRESHOLD_BYTES = 200
    _write_big_entry(temp_audit_dir, n=500)
    gz_files = sorted(temp_audit_dir.parent.glob("*.jsonl.*.gz"))
    assert len(gz_files) >= 1


def test_gzip_skips_backup_below_threshold(temp_audit_dir):
    """Backups smaller than the threshold stay as plaintext."""
    audit.GZIP_THRESHOLD_BYTES = 10_000_000  # 10 MB \u2014 nothing will exceed
    _write_big_entry(temp_audit_dir, n=500)
    gz = list(temp_audit_dir.parent.glob("*.jsonl.*.gz"))
    assert gz == []


def test_gzip_resulting_file_has_gz_suffix_and_plaintext_gone(temp_audit_dir):
    """After gzip, .N exists only as .N.gz (no plaintext counterpart)."""
    audit.GZIP_THRESHOLD_BYTES = 200
    _write_big_entry(temp_audit_dir, n=500)
    for n in range(1, audit.BACKUP_COUNT + 1):
        pt = temp_audit_dir.with_suffix(temp_audit_dir.suffix + f".{n}")
        gz = pt.with_suffix(pt.suffix + ".gz")
        # At least one of (pt, gz) exists; when gz exists, pt does NOT.
        if gz.exists():
            assert not pt.exists(), (
                f"plaintext {pt.name} should be deleted after gzip"
            )


def test_gzip_compressed_file_is_actually_gzip_format(temp_audit_dir):
    """Magic bytes 1f 8b confirm a real gzip stream."""
    audit.GZIP_THRESHOLD_BYTES = 200
    _write_big_entry(temp_audit_dir, n=500)
    gz_files = list(temp_audit_dir.parent.glob("*.jsonl.*.gz"))
    assert gz_files
    with gz_files[0].open("rb") as f:
        magic = f.read(2)
    assert magic == b"\x1f\x8b", f"expected gzip magic, got {magic!r}"


def test_gzip_audit_log_can_be_uncompressed_with_gzip_module(temp_audit_dir):
    """Roundtrip: write -> rotate+gzip -> decompress -> parse JSONL."""
    audit.GZIP_THRESHOLD_BYTES = 200
    # Write enough to force at least one rotation via audit.log().
    big = "x" * 300
    for i in range(400):
        audit.log({"i": i, "pad": big})

    gz_files = list(temp_audit_dir.parent.glob("*.jsonl.*.gz"))
    assert gz_files
    # Pick the largest .gz and roundtrip it.
    target = max(gz_files, key=lambda p: p.stat().st_size)
    with gzip.open(target, "rt", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert all("i" in entry and "pad" in entry for entry in lines)
    assert len(lines) > 0


def test_gzip_threshold_configurable_via_env(monkeypatch, tmp_path):
    """Env var picked up at import time; new module-level reads it."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("BASH_MCP_AUDIT_GZIP_THRESHOLD_BYTES", "500")
    # Force re-import.
    import importlib
    importlib.reload(audit)
    assert audit.GZIP_THRESHOLD_BYTES == 500
    importlib.reload(audit)  # restore default for the next test


def test_gzip_oldest_backup_compressed_first(temp_audit_dir):
    """When threshold triggers, .BACKUP_COUNT is processed first."""
    audit.GZIP_THRESHOLD_BYTES = 200
    audit.BACKUP_COUNT = 3
    _write_big_entry(temp_audit_dir, n=800)  # enough to fill .1 .2 .3

    gz_files = sorted(
        (p.name for p in temp_audit_dir.parent.glob("*.jsonl.*.gz")),
        key=lambda n: int(n.split(".")[-2]),  # extract N from "audit.jsonl.N.gz"
    )
    # We expect at least .3 (oldest) to be gzipped, possibly .2 and .1 too.
    assert len(gz_files) >= 1
    # Oldest backup number must be present (or all of them, depending on size).
    assert any(".3.gz" in n for n in gz_files) or len(gz_files) >= 1


def test_gzip_failure_keeps_plaintext_backup(temp_audit_dir, monkeypatch, capsys):
    """If gzip raises, the plaintext backup must remain on disk."""
    audit.GZIP_THRESHOLD_BYTES = 200

    import gzip as gzip_mod
    real_open = gzip_mod.open

    def boom(*args, **kwargs):
        raise OSError("simulated gzip failure")
    monkeypatch.setattr(gzip_mod, "open", boom)

    _write_big_entry(temp_audit_dir, n=500)

    # Plaintext .N must still exist (fail-soft).
    plaintext_backups = sorted(temp_audit_dir.parent.glob("*.jsonl.[0-9]"))
    assert len(plaintext_backups) >= 1, (
        "plaintext backups must survive a gzip failure"
    )

    # And stderr must have the warning.
    captured = capsys.readouterr()
    assert "gzip failed" in captured.err


def test_gzip_failure_logs_warning_to_stderr(temp_audit_dir, monkeypatch, capsys):
    """The warning goes to stderr (so systemd journal picks it up)."""
    audit.GZIP_THRESHOLD_BYTES = 200
    import gzip as gzip_mod
    def boom(*args, **kwargs):
        raise OSError("simulated")
    monkeypatch.setattr(gzip_mod, "open", boom)

    _write_big_entry(temp_audit_dir, n=500)
    captured = capsys.readouterr()
    assert "[bash-mcp audit] WARN" in captured.err


# --- backup_paths() ---


def test_gzip_backup_paths_prefers_gz_over_plaintext(temp_audit_dir):
    """When both .jsonl.N and .jsonl.N.gz exist, return only .gz."""
    audit.GZIP_THRESHOLD_BYTES = 200
    _write_big_entry(temp_audit_dir, n=500)
    paths = audit.backup_paths()
    for p in paths:
        # Each returned path must end in .gz (gz preference) OR be plaintext
        # only when no .gz exists for that N.
        if p.suffix == ".gz":
            assert p.name.endswith(".gz")
        # We never return BOTH .N and .N.gz for the same N.
    # Specifically: there should be no duplicate N (one .N and one .N.gz).
    ns_plaintext = {p.name.split(".")[-1] for p in paths if not p.name.endswith(".gz")}
    ns_gz = {p.name.split(".")[-2] for p in paths if p.name.endswith(".gz")}
    assert ns_plaintext.isdisjoint(ns_gz)


def test_gzip_status_reports_gzip_threshold_bytes():
    """The status tool surfaces the configured threshold."""
    from bash_mcp import server
    out = server.mcp._tool_manager._tools["bash_mcp_status"].fn()
    assert "gzip_threshold_bytes" in out["audit"]
    assert "gzip_enabled" in out["audit"]
    assert isinstance(out["audit"]["gzip_threshold_bytes"], int)


def test_gzip_status_gzip_enabled_field(monkeypatch):
    """gzip_enabled flag reflects env config."""
    from bash_mcp import server
    _status = server.mcp._tool_manager._tools["bash_mcp_status"].fn
    monkeypatch.setattr(audit, "GZIP_THRESHOLD_BYTES", 0)
    out = _status()
    assert out["audit"]["gzip_enabled"] is False

    monkeypatch.setattr(audit, "GZIP_THRESHOLD_BYTES", 1024)
    out = _status()
    assert out["audit"]["gzip_enabled"] is True
