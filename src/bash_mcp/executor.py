"""Bash subprocess executor.

Stateless. Each call spawns a fresh `bash -lc <command>` and waits up to
`timeout_ms` for completion. Stdout and stderr are captured and truncated.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from bash_mcp.concurrency import slot as concurrency_slot
from bash_mcp import project_allowlist


DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 600_000            # 10 min hard cap
MAX_OUTPUT_BYTES = 50_000           # truncate stdout/stderr beyond this
TRUNCATION_DIR = Path("/tmp/bash-mcp")


# cwd allowlist — every run_command call's cwd must resolve under one of these.
# Windows-style paths (C:\foo, D:/bar) are converted to /mnt/<drive>/... before this check.
ALLOWED_CWD_ROOTS: tuple[str, ...] = (
    os.path.expanduser("~"),
    "/tmp",
    "/home",
    "/mnt/c/Users/jesus",
    "/var/tmp",
)

# Windows drive-letter path: C:, D:, etc., with either \ or / separator.
_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    truncated: bool
    truncated_full_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "truncated_full_path": self.truncated_full_path,
        }


class BashNotFoundError(RuntimeError):
    pass


class InvalidCwdError(RuntimeError):
    pass


def _resolve_bash() -> str:
    bash = shutil.which("bash")
    if not bash:
        raise BashNotFoundError("bash not found on PATH")
    return bash


def _convert_windows_path(cwd: str) -> str:
    """Best-effort Windows-style path → WSL path.

    `C:\\Users\\foo\\bar` → `/mnt/c/Users/foo/bar`
    `D:/projects`        → `/mnt/d/projects`

    UNC paths, drive-relative paths, and `\\\\wsl$` are returned unchanged
    so the allowlist check rejects them with a clear message.
    """
    m = _WIN_DRIVE_RE.match(cwd)
    if not m:
        return cwd
    drive, rest = m.group(1).lower(), m.group(2)
    # Normalize separators to forward slashes for the WSL side.
    rest = rest.replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def _is_under_allowed_root(
    cwd: str,
    effective_roots: tuple[str, ...] | None = None,
) -> bool:
    """Check whether ``cwd`` is under one of the allowed roots.

    Args:
        cwd: The absolute cwd to check.
        effective_roots: When provided, check against this tuple
            instead of ``ALLOWED_CWD_ROOTS``. ``executor.run`` passes
            the per-project allowlist (via
            ``project_allowlist.effective_allowed_roots``) when a
            ``.bash-mcp.toml`` is in effect for the call's start dir;
            ``None`` means "fall back to the global allowlist" (the
            pre-v0.8 behavior).
    """
    s = str(cwd)
    roots = effective_roots if effective_roots is not None else ALLOWED_CWD_ROOTS
    for root in roots:
        if s == root or s.startswith(root + "/"):
            return True
    return False


def _truncate(s: str, label: str, audit_id: str) -> tuple[str, bool, str | None]:
    """Truncate `s` to MAX_OUTPUT_BYTES. On truncation, save the full copy to disk."""
    if len(s.encode("utf-8")) <= MAX_OUTPUT_BYTES:
        return s, False, None

    TRUNCATION_DIR.mkdir(parents=True, exist_ok=True)
    full_path = TRUNCATION_DIR / f"{audit_id}-{label}.out"
    full_path.write_text(s, encoding="utf-8")
    truncated = s.encode("utf-8")[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    suffix = f"\n[truncated at {MAX_OUTPUT_BYTES} bytes; full output at {full_path}]"
    return truncated + suffix, True, str(full_path)


def run(
    command: str,
    cwd: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    env: dict[str, str] | None = None,
    audit_id: str | None = None,
) -> ExecutionResult:
    """Execute `command` via `bash -lc`, returning a structured result.

    Raises:
        BashNotFoundError: bash binary missing.
        InvalidCwdError: cwd does not exist, is not a directory, or is not under an
            allowed root (see ALLOWED_CWD_ROOTS). Windows-style paths like
            `C:\\Users\\foo` are transparently converted to `/mnt/c/Users/foo`
            before the allowlist check.
        ValueError: timeout_ms out of range or empty command.
    """
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    if not (0 < timeout_ms <= MAX_TIMEOUT_MS):
        raise ValueError(f"timeout_ms must be in (0, {MAX_TIMEOUT_MS}], got {timeout_ms}")

    bash = _resolve_bash()

    # Resolve and validate cwd
    if cwd is None:
        effective_cwd = os.path.expanduser("~")
    else:
        # Step 1: convert Windows-style paths (C:\foo) to WSL (/mnt/c/foo).
        effective_cwd = _convert_windows_path(cwd)
        # Step 2: expand ~ if present.
        effective_cwd = os.path.expanduser(effective_cwd)
        # Step 3: if still not absolute (e.g. relative), make it absolute relative to HOME.
        if not os.path.isabs(effective_cwd):
            effective_cwd = os.path.abspath(
                os.path.join(os.path.expanduser("~"), effective_cwd)
            )

    # Step 4: allowlist check — BEFORE the existence check so we fail fast
    # with a clear message instead of "No such file or directory".
    # v0.8: per-project allowlist via .bash-mcp.toml. Walks up from
    # effective_cwd looking for the config; merges (extend) or replaces
    # the global roots. None = no project TOML = fall back to global.
    effective_roots = project_allowlist.effective_allowed_roots(
        effective_cwd, ALLOWED_CWD_ROOTS
    )
    if not _is_under_allowed_root(effective_cwd, effective_roots):
        if effective_roots is None:
            allowed_msg = ", ".join(ALLOWED_CWD_ROOTS)
        else:
            allowed_msg = ", ".join(effective_roots)
        raise InvalidCwdError(
            f"cwd not under an allowed root: {effective_cwd}; "
            f"allowed: {allowed_msg}"
        )

    # Step 5: existence + is_dir check.
    cwd_path = Path(effective_cwd)
    if not cwd_path.exists():
        raise InvalidCwdError(f"cwd does not exist: {effective_cwd}")
    if not cwd_path.is_dir():
        raise InvalidCwdError(f"cwd is not a directory: {effective_cwd}")

    # Build env: inherit + caller additions (caller wins)
    full_env = os.environ.copy()
    if env:
        for k, v in env.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError(f"env keys/values must be strings, got {k!r}: {v!r}")
            full_env[k] = v

    audit_id = audit_id or uuid.uuid4().hex
    started = time.monotonic()
    try:
        with concurrency_slot():
            completed = subprocess.run(
                [bash, "-lc", command],
                cwd=str(cwd_path),
                env=full_env,
                timeout=timeout_ms / 1000,
                capture_output=True,
                text=True,
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout, stdout_truncated, stdout_full = _truncate(
            completed.stdout or "", "stdout", audit_id
        )
        stderr, stderr_truncated, stderr_full = _truncate(
            completed.stderr or "", "stderr", audit_id
        )
        truncated = stdout_truncated or stderr_truncated
        full_path = stdout_full or stderr_full
        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=completed.returncode,
            duration_ms=duration_ms,
            timed_out=False,
            truncated=truncated,
            truncated_full_path=full_path,
        )
    except subprocess.TimeoutExpired as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_raw = (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr_raw = (e.stderr or b"").decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        stdout, _, _ = _truncate(stdout_raw, "stdout", audit_id)
        stderr, _, _ = _truncate(stderr_raw, "stderr", audit_id)
        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=-9,
            duration_ms=duration_ms,
            timed_out=True,
            truncated=False,
            truncated_full_path=None,
        )



# Explicit re-export list. v0.6 added the two underscore-prefixed helpers
# so that `bash_mcp.sessions` can reuse the allowlist + path conversion
# semantics without copying the logic.
__all__ = [
    "ALLOWED_CWD_ROOTS",
    "DEFAULT_TIMEOUT_MS",
    "MAX_TIMEOUT_MS",
    "MAX_OUTPUT_BYTES",
    "BashNotFoundError",
    "InvalidCwdError",
    "ExecutionResult",
    "run",
    # v0.6 — re-exported for sessions.py to reuse allowlist semantics:
    "_convert_windows_path",
    "_is_under_allowed_root",
]
