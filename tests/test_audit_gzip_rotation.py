"""Unit tests for the gzip-on-rotation lifecycle (v0.10).

The v0.7 gzip pass compressed a backup after each rotation, but the
rotation shift only renamed PLAINTEXT backups (`audit.jsonl.N`). Already
gzipped backups (`audit.jsonl.N.gz`) were never shifted, so they kept their
index — and the next gzip pass wrote a fresh `audit.jsonl.1.gz` over the
existing one. Net effect: with gzip enabled, **every rotation destroyed the
oldest backup**.

These tests pin the fixed lifecycle: successive rotations must preserve every
generation, at the right index.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from bash_mcp import audit


@pytest.fixture
def temp_audit_dir(monkeypatch, tmp_path):
    """Redirect XDG_DATA_HOME, lower the rotation threshold, enable gzip."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(audit, "_AUDIT_FILE", None)
    monkeypatch.setattr(audit, "MAX_AUDIT_BYTES", 1024)        # 1 KB
    monkeypatch.setattr(audit, "BACKUP_COUNT", 3)
    monkeypatch.setattr(audit, "GZIP_THRESHOLD_BYTES", 100)   # 100 B
    target = tmp_path / "bash-mcp" / "audit.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _force_rotation(path: Path, marker: str) -> None:
    """Force exactly one rotation whose archived content carries `marker`.

    The big direct write pushes the active log over MAX_AUDIT_BYTES; the
    following audit.log() triggers the rotation (rotation only happens
    inside log()).
    """
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"gen": marker}) + "\n")
        f.write("x" * 2000 + "\n")
    # The rotation entry itself must NOT carry the marker: it is written
    # to the fresh log and would leak into the next generation's backup.
    audit.log({"tool": "rotate"})


def _gz_text(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return f.read()


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob("audit.jsonl.*"))


# --- the data-loss regression ---


def test_second_rotation_preserves_the_first_gzipped_backup(temp_audit_dir):
    """The generation archived by rotation 1 must still exist after rotation 2."""
    _force_rotation(temp_audit_dir, "GEN1")
    _force_rotation(temp_audit_dir, "GEN2")

    gz1 = temp_audit_dir.parent / "audit.jsonl.1.gz"
    gz2 = temp_audit_dir.parent / "audit.jsonl.2.gz"
    assert gz1.exists(), f"expected {gz1.name}, found {_backups(temp_audit_dir)}"
    assert gz2.exists(), f"expected {gz2.name}, found {_backups(temp_audit_dir)}"

    assert "GEN2" in _gz_text(gz1)
    assert "GEN1" in _gz_text(gz2), "the oldest generation was destroyed"
    assert "GEN2" not in _gz_text(gz2)


def test_third_rotation_keeps_three_distinct_generations(temp_audit_dir):
    """Three rotations must leave three distinct generations on disk."""
    _force_rotation(temp_audit_dir, "GEN1")
    _force_rotation(temp_audit_dir, "GEN2")
    _force_rotation(temp_audit_dir, "GEN3")

    seen = {}
    for n in (1, 2, 3):
        gz = temp_audit_dir.parent / f"audit.jsonl.{n}.gz"
        assert gz.exists(), f"expected {gz.name}, found {_backups(temp_audit_dir)}"
        seen[n] = _gz_text(gz)

    assert "GEN3" in seen[1]
    assert "GEN2" in seen[2]
    assert "GEN1" in seen[3]
    assert "GEN1" not in seen[1] and "GEN1" not in seen[2]


def test_oldest_backup_is_dropped_in_both_variants(temp_audit_dir):
    """Beyond BACKUP_COUNT the oldest generation is dropped, not piled up."""
    for i in range(1, 6):
        _force_rotation(temp_audit_dir, f"GEN{i}")

    backups = _backups(temp_audit_dir)
    assert len(backups) <= audit.BACKUP_COUNT, f"too many backups: {backups}"
    assert not (temp_audit_dir.parent / "audit.jsonl.4.gz").exists()


def test_read_backup_returns_the_older_generation_after_a_rotation(temp_audit_dir):
    """The read-back tool must see the archived (older) generation."""
    _force_rotation(temp_audit_dir, "GEN1")
    _force_rotation(temp_audit_dir, "GEN2")

    entries = audit.read_backup(2)
    assert entries, "expected entries in backup 2"
    assert any(e.get("gen") == "GEN1" for e in entries), entries[:3]


def test_backup_paths_lists_the_gzipped_backups(temp_audit_dir):
    _force_rotation(temp_audit_dir, "GEN1")
    _force_rotation(temp_audit_dir, "GEN2")

    paths = audit.backup_paths()
    names = [p.name for p in paths]
    assert "audit.jsonl.1.gz" in names, names
    assert "audit.jsonl.2.gz" in names, names


# --- the gzip pass never destroys an existing archive ---


def test_gzip_pass_never_overwrites_an_existing_gzipped_file(temp_audit_dir):
    """A pre-existing .gz must survive the sweep untouched."""
    gz = temp_audit_dir.parent / "audit.jsonl.1.gz"
    gz.write_bytes(gzip.compress(b"PRECIOUS-ARCHIVE\n"))
    plain = temp_audit_dir.parent / "audit.jsonl.1"
    # Must be larger than GZIP_THRESHOLD_BYTES (100) or the sweep skips it
    # and the test would pass without exercising the overwrite path.
    plain.write_text("newer plaintext " + "y" * 500 + "\n", encoding="utf-8")

    audit._gzip_pass(temp_audit_dir)

    assert _gz_text(gz).strip() == "PRECIOUS-ARCHIVE", "the archive was clobbered"
    assert plain.exists(), "the plaintext backup must not be deleted either"


# --- threshold default ---


def test_default_gzip_threshold_is_one_mib():
    """v0.10 turns the gzip pass on by default, above a 1 MiB threshold."""
    assert audit.DEFAULT_GZIP_THRESHOLD_BYTES == 1024 * 1024


def test_gzip_enabled_by_default_in_status(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(audit, "_AUDIT_FILE", None)
    from bash_mcp import server

    out = server.mcp._tool_manager._tools["bash_mcp_status"].fn()
    assert out["audit"]["gzip_enabled"] is True
    assert out["audit"]["gzip_threshold_bytes"] == 1024 * 1024


def test_status_reports_gzip_and_plaintext_backup_counts(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(audit, "_AUDIT_FILE", None)
    from bash_mcp import server

    out = server.mcp._tool_manager._tools["bash_mcp_status"].fn()
    assert "gzip_backups" in out["audit"]
    assert "plaintext_backups" in out["audit"]
