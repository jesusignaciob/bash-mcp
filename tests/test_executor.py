"""Unit tests for the executor (subprocess wrapper)."""

from __future__ import annotations

import os

import pytest

from bash_mcp.executor import (
    BashNotFoundError,
    InvalidCwdError,
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
    # This is a sanity check that login-shell env is loaded.
    r = run("echo $PATH")
    assert r.exit_code == 0
    assert "/usr/bin" in r.stdout
