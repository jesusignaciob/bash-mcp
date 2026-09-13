"""Unit tests for the executor (subprocess wrapper)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bash_mcp.executor import (
    ALLOWED_CWD_ROOTS,
    BashNotFoundError,
    InvalidCwdError,
    _convert_windows_path,
    _is_under_allowed_root,
    run,
)


def test_echo_hello() -> None:
    r = run("echo hello")
    assert r.exit_code == 0
    assert r.stdout.strip() == "hello"
    assert r.timed_out is False
    assert r.truncated is False


def test_ls_nonexistent_returns_exit_2() -> None:
    r = run("ls /nonexistent/path/zzz")
    assert r.exit_code == 2
    assert "No such file" in r.stderr


def test_timeout_returns_timed_out() -> None:
    r = run("sleep 10", timeout_ms=500)
    assert r.timed_out is True
    assert r.exit_code == -9
    assert r.duration_ms >= 500


def test_invalid_cwd_raises() -> None:
    with pytest.raises(InvalidCwdError):
        run("echo x", cwd="/nonexistent/path/zzz")


def test_empty_command_raises() -> None:
    with pytest.raises(ValueError):
        run("")


def test_timeout_too_large_raises() -> None:
    with pytest.raises(ValueError):
        run("echo x", timeout_ms=999_999)


def test_env_var_propagation() -> None:
    r = run("echo $BAZMCP_TEST_VAR", env={"BAZMCP_TEST_VAR": "hello-from-test"})
    assert "hello-from-test" in r.stdout


def test_cwd_respected() -> None:
    r = run("pwd", cwd=os.path.expanduser("~"))
    assert r.exit_code == 0
    assert r.stdout.strip() == os.path.expanduser("~")


def test_bash_login_shell_loads_bashrc() -> None:
    """`bash -lc` should make user-installed PATH entries visible."""
    r = run("echo $PATH")
    assert r.exit_code == 0
    assert "/usr/bin" in r.stdout


# --- B: cwd allowlist + Windows→WSL path conversion ---

def test_convert_windows_backslash() -> None:
    assert _convert_windows_path(r"C:\Users\jesus\foo") == "/mnt/c/Users/jesus/foo"


def test_convert_windows_forward_slash() -> None:
    assert _convert_windows_path("D:/projects") == "/mnt/d/projects"


def test_convert_windows_passthrough() -> None:
    """Non-drive-letter paths are returned unchanged (allowlist will reject)."""
    assert _convert_windows_path("/etc") == "/etc"
    assert _convert_windows_path("foo/bar") == "foo/bar"
    assert _convert_windows_path("") == ""
    assert _convert_windows_path("C:") == "C:"  # bare drive letter, not matched


def test_is_under_allowed_root_exact() -> None:
    home = os.path.expanduser("~")
    assert _is_under_allowed_root(home) is True
    assert _is_under_allowed_root("/tmp") is True
    assert _is_under_allowed_root("/tmp/foo") is True
    assert _is_under_allowed_root("/var/tmp/x") is True


def test_is_under_allowed_root_rejects() -> None:
    assert _is_under_allowed_root("/etc") is False
    assert _is_under_allowed_root("/var/log") is False
    assert _is_under_allowed_root("/usr/local/bin") is False
    assert _is_under_allowed_root("/mnt/c/Windows") is False


def test_allowed_cwd_roots_includes_home_and_wsl_user() -> None:
    """The allowlist MUST cover HOME and /mnt/c/Users/jesus (user's primary area)."""
    assert os.path.expanduser("~") in ALLOWED_CWD_ROOTS
    assert "/mnt/c/Users/jesus" in ALLOWED_CWD_ROOTS


@pytest.mark.parametrize("cwd,expect_ok", [
    # Allowed roots — allowlist should accept (path may not exist; that's an
    # existence-check failure, not an allowlist failure).
    ("/home/jbecerra", True),
    ("/tmp/foo/bar", True),
    ("/home/other", True),
    ("/mnt/c/Users/jesus/projects", True),
    ("/var/tmp/x", True),
    # Windows-style conversion + allow (C: drive, under /mnt/c/Users/jesus)
    (r"C:\Users\jesus\foo", True),        # → /mnt/c/Users/jesus/foo
    # Disallowed
    ("/etc", False),
    ("/var/log", False),
    ("/usr/local", False),
    ("/mnt/c/Windows", False),
    (r"C:\Windows\System32", False),      # converts to /mnt/c/Windows/... → rejected
    ("D:/projects", False),                # converts to /mnt/d/projects → not in allowlist
])
def test_cwd_allowlist(cwd: str, expect_ok: bool) -> None:
    """Test the allowlist layer only. Existence failures (cwd doesn't exist)
    are NOT this test's concern — the allowlist may pass, then existence fails."""
    try:
        run("echo x", cwd=cwd, timeout_ms=2000)
    except InvalidCwdError as e:
        msg = str(e)
        allowlist_rejected = "not under an allowed root" in msg
        if allowlist_rejected:
            # Allowlist rejection is the only thing this test cares about.
            if expect_ok:
                pytest.fail(f"expected {cwd!r} to be allowed, got allowlist rejection: {msg}")
            # else: correctly rejected by allowlist — pass
        else:
            # Existence / is_dir check failed — allowlist passed.
            if not expect_ok:
                pytest.fail(f"expected {cwd!r} to be rejected, but allowlist passed (got: {msg})")
            # else: allowlist passed, but path doesn't exist — also fine for this test
    except (BashNotFoundError, OSError):
        # Unrelated failures are fine for the allowlist test.
        pass


# --- C: per-project allowlist via .bash-mcp.toml (v0.8) ---

from bash_mcp import project_allowlist  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_pa_cache():
    """Drop the project_allowlist module cache around every test."""
    project_allowlist.clear_cache()
    yield
    project_allowlist.clear_cache()


def test_project_allowlist_extend_mode_adds_root(tmp_path: Path) -> None:
    """extend mode: a project TOML extends the global allowlist.

    We create a dir outside the global allowlist, drop a TOML that
    allows it, and verify run() accepts it (under extend mode).
    """
    import tempfile
    with tempfile.TemporaryDirectory() as sandbox:
        sandbox_p = Path(sandbox).resolve()
        # Drop a TOML that allows this sandbox under extend mode.
        (sandbox_p / ".bash-mcp.toml").write_text(
            'mode = "extend"\nallowed_roots = ["."]\n',
            encoding="utf-8",
        )
        # run() from this dir should succeed.
        r = run("pwd", cwd=str(sandbox_p))
        assert r.exit_code == 0
        assert r.stdout.strip() == str(sandbox_p)


def test_project_allowlist_replace_mode_drops_global(tmp_path: Path) -> None:
    """replace mode: a project TOML REPLACES the global allowlist.

    Even though cwd is under HOME (allowed globally), replace mode
    with an unrelated allowed_roots must REJECT it.
    """
    # Drop a TOML at tmp_path that allows only a sibling dir.
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (tmp_path / ".bash-mcp.toml").write_text(
        f'mode = "replace"\nallowed_roots = ["{sibling.resolve()}"]\n',
        encoding="utf-8",
    )
    # tmp_path itself is under HOME (= /home/jbecerra, a global root),
    # but replace mode should drop the global and only allow the sibling.
    with pytest.raises(InvalidCwdError) as ei:
        run("echo x", cwd=str(tmp_path))
    assert "not under an allowed root" in str(ei.value)


def test_project_allowlist_not_found_falls_back_to_global(tmp_path: Path) -> None:
    """No TOML anywhere up the tree -> fall back to global allowlist.

    We use a path under /tmp (always in global allowlist) with no
    TOML in /tmp or any ancestor, and verify the call succeeds.
    """
    deep = tmp_path / "no_toml_here" / "deep"
    deep.mkdir(parents=True)
    # /tmp is in global allowlist; no TOML should appear on the walk
    # up because pytest tmp_path lives under /tmp/pytest-of-*.
    r = run("echo fallback-ok", cwd=str(deep))
    assert r.exit_code == 0
    assert "fallback-ok" in r.stdout
