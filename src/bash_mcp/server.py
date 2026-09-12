"""bash-mcp: WSL bash executor MCP server.

Phase 0 — Project skeleton (echo).
Phase 1 — run_command MVP + check_env.
Phase 2 — list_binaries + which.
v0.2   — cwd allowlist (executor.py), error hints (make_error_response),
        bash_mcp_status tool.
"""

import argparse
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from bash_mcp import __version__, audit
from bash_mcp import discovery
from bash_mcp.concurrency import MAX_CONCURRENT, active as concurrency_active, slot as concurrency_slot
from bash_mcp.executor import (
    ALLOWED_CWD_ROOTS,
    DEFAULT_TIMEOUT_MS,
    BashNotFoundError,
    ExecutionResult,
    InvalidCwdError,
    run as exec_run,
)
from bash_mcp.safety import Class, Classification, classify

mcp = FastMCP("bash-mcp")

# Module-level state for the status tool
_START_TIME = time.time()
_AUDIT_PATH = Path.home() / ".local" / "share" / "bash-mcp" / "audit.jsonl"

# Documentation path for hints — points the agent to the skill
_SKILL_DOC_PATH = "C:\\Users\\jesus\\.mavis\\skills\\bash-mcp\\SKILL.md"


# --- Error envelope helpers (extracted for testability + reuse) ---

def make_error_response(
    code: str,
    message: str,
    hint: str,
    matched_pattern: str | None = None,
    audit_id: str | None = None,
    classification: dict | None = None,
) -> dict[str, Any]:
    """Build a standardized error envelope.

    Every error response from this server has the same shape:
        {
          "error": {
            "code": "<machine-readable>",
            "message": "<human-readable description>",
            "hint": "<actionable next step>",
            "documentation": "<path to skill or docs>",
            "matched_pattern": "<regex that fired>" (if applicable),
          },
          "audit_id": "<id>" (if applicable),
          "classification": {...} (if applicable),
        }

    Testable in isolation (tests/test_errors.py).
    """
    err: dict[str, Any] = {
        "code": code,
        "message": message,
        "hint": hint,
        "documentation": _SKILL_DOC_PATH + "#safety",
    }
    if matched_pattern is not None:
        err["matched_pattern"] = matched_pattern
    out: dict[str, Any] = {"error": err}
    if audit_id is not None:
        out["audit_id"] = audit_id
    if classification is not None:
        out["classification"] = classification
    return out


def _hint_for_invalid_cwd(cwd_attempted: str) -> str:
    return (
        f"cwd '{cwd_attempted}' is not under an allowed root. "
        f"Allowed roots: {', '.join(ALLOWED_CWD_ROOTS)}. "
        "If the path is a Windows-style path (C:\\foo), it is automatically converted "
        "to /mnt/c/foo. Otherwise, pass a path under one of the allowed roots."
    )


@mcp.tool
def echo(message: str) -> str:
    """Echo a message back. Smoke-test tool."""
    return message


@mcp.tool
def bash_run_command(
    command: str,
    cwd: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    env: dict[str, str] | None = None,
    dangerous: bool = False,
) -> dict[str, Any]:
    """Execute a bash command inside WSL and return structured output.

    This is the **REQUIRED** way to run WSL commands. DO NOT use
    `wsl -d ... -- bash -c "..."` from PowerShell — that path has quoting,
    UTF-16, and PATH bugs. Call this tool instead.

    Args:
        command: The bash command line (single string). Runs via `bash -lc`,
            so login-shell env (~/.bashrc) is loaded.
        cwd: Absolute path to working directory. Must be under one of the
            allowed roots (see hint on INVALID_CWD error). Defaults to $HOME.
        timeout_ms: Hard cap on execution time. Default 30000 (30s).
            Max 600000 (10 min).
        env: Additional env vars to merge into the inherited environment
            (caller values win on conflict).
        dangerous: If True, bypasses the SOFT denylist (sudo, kill -9, etc.).
            The HARD denylist (rm -rf /, dd of=/dev/sd*, fork bombs) is
            ALWAYS enforced and cannot be bypassed.

    Returns:
        On success: {stdout, stderr, exit_code, duration_ms, timed_out,
                     truncated, classification, audit_id}
        On rejection: {error: {code, message, hint, documentation, ...}}

    Safety: HARD denylist cannot be bypassed. SOFT denylist requires dangerous=true.
    """
    audit_id = audit.new_audit_id()

    # Step 1: classify
    cls: Classification = classify(command)
    if cls.cls == Class.REJECT:
        audit.log({
            "ts": audit_id.split("-")[0] if "-" in audit_id else "",
            "audit_id": audit_id,
            "tool": "bash_run_command",
            "args": {"command": command, "cwd": cwd, "timeout_ms": timeout_ms,
                      "env_keys": sorted((env or {}).keys()), "dangerous": dangerous},
            "classification": cls.to_dict(),
            "outcome": "REJECTED",
            "reason": "matched hard denylist",
        })
        return make_error_response(
            code="FORBIDDEN_COMMAND",
            message="Command matches the hard denylist and cannot be executed.",
            hint=(
                "This pattern cannot be bypassed even with dangerous=true. "
                "Modify the command to avoid the dangerous fragment "
                "(e.g. use /tmp or /home instead of / for rm targets; "
                "do not pipe curl/wget directly into bash)."
            ),
            matched_pattern=cls.matched_pattern,
            audit_id=audit_id,
            classification=cls.to_dict(),
        )

    if cls.cls == Class.DANGEROUS and not dangerous:
        audit.log({
            "ts": audit_id.split("-")[0] if "-" in audit_id else "",
            "audit_id": audit_id,
            "tool": "bash_run_command",
            "args": {"command": command, "cwd": cwd, "timeout_ms": timeout_ms,
                      "env_keys": sorted((env or {}).keys()), "dangerous": dangerous},
            "classification": cls.to_dict(),
            "outcome": "DENIED",
            "reason": "matched soft denylist; dangerous flag not set",
        })
        return make_error_response(
            code="DANGEROUS_COMMAND_REQUIRES_OVERRIDE",
            message="Command matches the soft denylist and was not authorized.",
            hint=(
                "Retry with dangerous=true if this is intentional, OR remove the "
                "dangerous fragment (e.g. drop sudo, use --no-force, drop kill -9)."
            ),
            matched_pattern=cls.matched_pattern,
            audit_id=audit_id,
            classification=cls.to_dict(),
        )

    # Step 2: execute
    try:
        result: ExecutionResult = exec_run(
            command=command,
            cwd=cwd,
            timeout_ms=timeout_ms,
            env=env,
            audit_id=audit_id,
        )
    except BashNotFoundError as e:
        return make_error_response(
            code="BASH_NOT_FOUND",
            message=str(e),
            hint="bash binary not on PATH. Check the WSL environment.",
            audit_id=audit_id,
        )
    except InvalidCwdError as e:
        return make_error_response(
            code="INVALID_CWD",
            message=str(e),
            hint=_hint_for_invalid_cwd(cwd or "$HOME"),
            audit_id=audit_id,
        )
    except ValueError as e:
        return make_error_response(
            code="INVALID_ARGUMENT",
            message=str(e),
            hint="Check timeout_ms range (1..600000) and that command is non-empty.",
            audit_id=audit_id,
        )

    # Step 3: audit
    audit.log_command(
        tool="bash_run_command",
        args={
            "command": command,
            "cwd": cwd,
            "timeout_ms": timeout_ms,
            "env_keys": sorted((env or {}).keys()),
            "dangerous": dangerous,
        },
        classification=cls.to_dict(),
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        stdout_bytes=len((result.stdout or "").encode("utf-8")),
        stderr_bytes=len((result.stderr or "").encode("utf-8")),
        timed_out=result.timed_out,
        truncated=result.truncated,
        audit_id=audit_id,
    )

    # Step 4: return
    payload = result.to_dict()
    payload["classification"] = cls.to_dict()
    payload["audit_id"] = audit_id
    if dangerous and cls.cls == Class.DANGEROUS:
        payload["warning"] = "Executed with dangerous=true override"
    return payload


@mcp.tool
def bash_check_env() -> dict[str, Any]:
    """Return OS / shell / tooling environment info.

    Read-only. Does NOT execute any subprocess beyond what `platform` and
    `shutil.which` already do internally.

    Returns:
        {os, kernel, python_version, uv_version, home, cwd, path_entries}
    """
    info: dict[str, Any] = {
        "os": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "home": os.path.expanduser("~"),
        "cwd": os.getcwd(),
    }

    uv = shutil.which("uv")
    info["uv_path"] = uv
    if uv:
        import subprocess
        try:
            with concurrency_slot():
                r = subprocess.run(
                    [uv, "--version"],
                    capture_output=True, text=True, timeout=2,
                )
            info["uv_version"] = (r.stdout or r.stderr).strip()
        except Exception:
            pass

    bash = shutil.which("bash")
    info["bash_path"] = bash
    if bash:
        import subprocess
        try:
            with concurrency_slot():
                r = subprocess.run(
                    [bash, "--version"],
                    capture_output=True, text=True, timeout=2,
                )
            first = (r.stdout or r.stderr).splitlines()[0] if (r.stdout or r.stderr) else ""
            info["bash_version"] = first
        except Exception:
            pass

    path = os.environ.get("PATH", "")
    info["path_entries"] = path.split(os.pathsep)[:20]
    info["path_count"] = len(path.split(os.pathsep))

    return info


@mcp.tool
def bash_list_binaries() -> list[dict[str, Any]]:
    """List well-known binaries and whether each is installed.

    Returns:
        Array of {name, path, exists, version?} for each known tool,
        sorted by installed-first then alphabetical. Use `bash_which`
        for binaries not in this list.
    """
    return discovery.list_binaries()


@mcp.tool
def bash_which(name: str) -> dict[str, Any]:
    """Resolve a specific binary by name.

    Args:
        name: Binary name (e.g. "rg", "fnm").

    Returns:
        {name, path, exists, version?}
        - `path` is the absolute path or null if not found.
        - `exists` is true iff the binary is on PATH.
        - `version` is the first line of `<bin> --version`, truncated to 200 chars.
    """
    return discovery.which(name)


@mcp.tool
def bash_mcp_status() -> dict[str, Any]:
    """Return health/stats for the bash-mcp service.

    Read-only, no subprocess. Useful for verifying the service is up,
    how many calls have been audit-logged, and what's installed.

    Returns:
        {service, version, uptime_seconds, start_time,
         audit: {path, entries, size_bytes},
         python_version, process: {pid, rss_bytes}, tools: [...]}
    """
    audit_size = 0
    audit_entries = 0
    if _AUDIT_PATH.exists():
        audit_size = _AUDIT_PATH.stat().st_size
        with _AUDIT_PATH.open("rb") as f:
            for _ in f:
                audit_entries += 1

    pid = os.getpid()
    rss_bytes: int | None = None
    try:
        import psutil  # noqa: F401 — optional, falls back below
        rss_bytes = psutil.Process(pid).memory_info().rss
    except ImportError:
        # Fallback: read /proc/self/status directly (Linux only).
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        # VmRSS is in kB
                        rss_bytes = int(line.split()[1]) * 1024
                        break
        except (OSError, ValueError):
            pass

    return {
        "service": "active",
        "version": __version__,
        "uptime_seconds": int(time.time() - _START_TIME),
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_START_TIME)),
        "audit": {
            "path": str(_AUDIT_PATH),
            "entries": audit_entries,
            "size_bytes": audit_size,
            "max_bytes": audit.MAX_AUDIT_BYTES,
            "backup_count": audit.BACKUP_COUNT,
            "backups_present": [p.name for p in audit.backup_paths()],
        },
        "concurrency": {
            "max_concurrent": MAX_CONCURRENT,
            "active": concurrency_active(),
        },
        "python_version": platform.python_version(),
        "process": {
            "pid": pid,
            "rss_bytes": rss_bytes,
        },
        "tools": [
            "bash_run_command",
            "bash_check_env",
            "bash_list_binaries",
            "bash_which",
            "bash_mcp_status",
            "echo",
        ],
    }


def main() -> None:
    """Entry point for `bash-mcp` console script (see pyproject.toml)."""
    parser = argparse.ArgumentParser(description="bash-mcp — WSL bash executor")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="streamable-http",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("FASTMCP_SERVER_HOST", "0.0.0.0"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FASTMCP_SERVER_PORT", "54321")),
    )
    args = parser.parse_args()

    mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
