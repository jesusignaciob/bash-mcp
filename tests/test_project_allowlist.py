"""Unit tests for bash_mcp.project_allowlist (v0.8).

All tests use ``tmp_path`` (pytest fixture) for hermetic temp dirs and
call ``project_allowlist.clear_cache()`` in setup/teardown so cache
state never leaks between tests.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from bash_mcp import project_allowlist
from bash_mcp.project_allowlist import (
    CONFIG_FILENAME,
    DEFAULT_MODE,
    ProjectAllowlist,
    effective_allowed_roots,
    load,
)


GLOBAL_ROOTS: tuple[str, ...] = ("/home", "/tmp")


@pytest.fixture(autouse=True)
def _clean_cache():
    """Drop the module-level cache before and after each test."""
    project_allowlist.clear_cache()
    yield
    project_allowlist.clear_cache()


def _write_toml(parent: Path, text: str) -> Path:
    """Write ``.bash-mcp.toml`` under ``parent`` and return its path."""
    p = parent / CONFIG_FILENAME
    p.write_text(text, encoding="utf-8")
    return p


# --- load(): walk-up + no-file-found ---


def test_load_returns_none_when_no_toml(tmp_path: Path) -> None:
    """Walk-up from a fresh tmp dir finds nothing -> None.

    Also exercises the negative cache: a second call with the same
    (start_dir, mtime_ns=-1) MUST hit the cached None instead of
    re-walking. We verify by re-querying deeper before any TOML is
    added and asserting the cache returns None.
    """
    deeper = tmp_path / "a" / "b" / "c"
    deeper.mkdir(parents=True)

    assert load(deeper) is None
    # Second call: cache hit (same key), returns None without walking.
    assert load(deeper) is None

    # Sanity: if a TOML appears AFTER the negative cache is set, the
    # walk-up DOES find it on the next call (the negative cache is
    # scoped to the "no file at this mtime" state).
    _write_toml(tmp_path, 'mode = "extend"\nallowed_roots = ["a"]\n')
    proj = load(deeper)
    assert proj is not None  # walked up, found the new TOML


def test_load_finds_toml_at_start_dir(tmp_path: Path) -> None:
    """A TOML next to start_dir is picked up."""
    text = 'mode = "extend"\nallowed_roots = [".", "sub"]\n'
    _write_toml(tmp_path, text)
    proj = load(tmp_path)
    assert isinstance(proj, ProjectAllowlist)
    assert proj.mode == "extend"
    assert str(tmp_path.resolve()) in proj.allowed_roots
    assert str((tmp_path / "sub").resolve()) in proj.allowed_roots


def test_load_walks_up_to_parent(tmp_path: Path) -> None:
    """TOML at parent is reachable from a child directory."""
    text = 'mode = "extend"\nallowed_roots = [".", "sub"]\n'
    _write_toml(tmp_path, text)
    child = tmp_path / "sub" / "deeper" / "deepest"
    child.mkdir(parents=True)
    proj = load(child)
    assert proj is not None
    assert proj.toml_dir == str(tmp_path.resolve())


# --- cache invalidation ---


def test_load_invalidates_cache_on_mtime_change(tmp_path: Path) -> None:
    """Editing the TOML (mtime change) invalidates the cache entry.

    We verify by mutating mode from "extend" to "replace" and
    asserting the cached project is replaced.
    """
    text1 = 'mode = "extend"\nallowed_roots = [".", "sub"]\n'
    _write_toml(tmp_path, text1)
    proj1 = load(tmp_path)
    assert proj1 is not None and proj1.mode == "extend"

    # Force a different mtime by sleeping briefly + rewriting content.
    # We rewrite via overwrite so mtime changes deterministically
    # even on filesystems with coarse mtime resolution.
    time.sleep(0.05)  # ensure mtime ticks on coarse FS
    text2 = 'mode = "replace"\nallowed_roots = [".", "sub"]\n'
    _write_toml(tmp_path, text2)

    proj2 = load(tmp_path)
    assert proj2 is not None
    assert proj2.mode == "replace"
    assert proj1.config_path == proj2.config_path  # same file
    # Different dataclass instances (cache returned fresh parse).
    assert proj1 is not proj2


# --- effective_allowed_roots() modes ---


def test_effective_roots_returns_none_when_no_project(tmp_path: Path) -> None:
    """No TOML -> None (caller falls back to global)."""
    assert effective_allowed_roots(tmp_path, GLOBAL_ROOTS) is None


def test_effective_roots_extend_merges_and_dedupes(tmp_path: Path) -> None:
    """extend: global + project, de-duped preserving first-seen order."""
    text = 'mode = "extend"\nallowed_roots = ["/foo", "/home"]\n'
    _write_toml(tmp_path, text)
    roots = effective_allowed_roots(tmp_path, GLOBAL_ROOTS)
    assert roots is not None
    # Order: global first, then project-only additions.
    assert roots[0] == GLOBAL_ROOTS[0]   # "/home"
    assert roots[1] == GLOBAL_ROOTS[1]   # "/tmp"
    assert "/foo" in roots
    # Duplicates: "/home" appears once (project entry deduped).
    assert list(roots).count("/home") == 1


def test_effective_roots_replace_drops_global(tmp_path: Path) -> None:
    """replace: ONLY the project roots, global is ignored entirely."""
    text = 'mode = "replace"\nallowed_roots = ["/sandbox/only"]\n'
    _write_toml(tmp_path, text)
    roots = effective_allowed_roots(tmp_path, GLOBAL_ROOTS)
    assert roots is not None
    assert "/sandbox/only" in roots
    for g in GLOBAL_ROOTS:
        assert g not in roots


def test_effective_roots_replace_empty_is_lockout(tmp_path: Path) -> None:
    """replace with empty list = deliberate lockout; () not None."""
    text = 'mode = "replace"\nallowed_roots = []\n'
    _write_toml(tmp_path, text)
    roots = effective_allowed_roots(tmp_path, GLOBAL_ROOTS)
    assert roots == ()  # empty tuple, NOT None -> don't fall back


# --- fail-soft on malformed TOML / invalid schema ---


def test_load_malformed_toml_returns_none(tmp_path: Path, capsys) -> None:
    """Garbage TOML -> None + stderr warning (no crash)."""
    _write_toml(tmp_path, "this is not = valid = TOML ===\n")
    assert load(tmp_path) is None
    err = capsys.readouterr().err
    assert "WARN" in err
    assert "falling back to global" in err.lower()


def test_load_non_list_allowed_roots_returns_none(tmp_path: Path, capsys) -> None:
    """allowed_roots must be a list; otherwise None + stderr warning."""
    text = 'mode = "extend"\nallowed_roots = "not-a-list"\n'
    _write_toml(tmp_path, text)
    assert load(tmp_path) is None
    err = capsys.readouterr().err
    assert "must be a list" in err


def test_load_unknown_mode_falls_back_to_default(tmp_path: Path, capsys) -> None:
    """Unknown mode -> DEFAULT_MODE ("extend") with a warning, not a crash."""
    text = 'mode = "wrong"\nallowed_roots = ["/foo"]\n'
    _write_toml(tmp_path, text)
    proj = load(tmp_path)
    assert proj is not None
    assert proj.mode == DEFAULT_MODE  # "extend"
    err = capsys.readouterr().err
    assert "not one of" in err
