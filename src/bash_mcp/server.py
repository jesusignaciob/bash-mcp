"""bash-mcp: WSL bash executor MCP server.

Phase 0 — Project skeleton (echo).
Phase 1 — run_command MVP + check_env.
Phase 2 — list_binaries + which.
"""

import argparse
import os
import platform
import shutil
from typing import Any

from fastmcp import FastMCP

from bash_mcp import audit
from bash_mcp import discovery
from bash_mcp.executor import (
    DEFAULT_TIMEOUT_MS,
    BashNotFoundError,
    ExecutionResult,
    InvalidCwdError,
    run as exec_run,
)
from bash_mcp.safety import Class, Classification, classify

mcp = FastMCP("bash-mcp")


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
        cwd: Absolute path to working directory. Defaults to $HOME.
        timeout_ms: Hard cap on execution time. Default 30000 (30s).
            Max 600000 (10 min).
        env: Additional env vars to merge into the inherited environment
            (caller values win on conflict).
        dangerous: If True, bypasses the SOFT denylist (sudo, kill -9, etc.).
            The HARD denylist (rm -rf /, dd of=/dev/sd*, fork bombs) is
            ALWAYS enforced and cannot be bypassed.

    Returns:
        On success:
            {stdout, stderr, exit_code, duration_ms, timed_out,
             truncated, classification: {class, matched_pattern},
             audit_id}

        On rejection:
            {error: {code: "FORBIDDEN_COMMAND" | "DANGEROUS_REQUIRES_OVERRIDE",
                     message: str, matched_pattern: str}}

    Safety:
        Commands matching the HARD denylist are always rejected.
        Commands matching the SOFT denylist require `dangerous=true`.
    """
    audit_id = audit.new_audit_id()

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
        return {
            "error": {
                "code": "FORBIDDEN_COMMAND",
                "message": "Command matches the hard denylist and cannot be executed.",
                "matched_pattern": cls.matched_pattern,
            },
            "audit_id": audit_id,
            "classification": cls.to_dict(),
        }

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
        return {
            "error": {
                "code": "DANGEROUS_COMMAND_REQUIRES_OVERRIDE",
                "message": (
                    "Command matches the soft denylist. "
                    "Retry with dangerous=true if this is intentional."
                ),
                "matched_pattern": cls.matched_pattern,
            },
            "audit_id": audit_id,
            "classification": cls.to_dict(),
        }

    try:
        result: ExecutionResult = exec_run(
            command=command,
            cwd=cwd,
            timeout_ms=timeout_ms,
            env=env,
            audit_id=audit_id,
        )
    except BashNotFoundError as e:
        return {"error": {"code": "BASH_NOT_FOUND", "message": str(e)}, "audit_id": audit_id}
    except InvalidCwdError as e:
        return {"error": {"code": "INVALID_CWD", "message": str(e)}, "audit_id": audit_id}
    except ValueError as e:
        return {"error": {"code": "INVALID_ARGUMENT", "message": str(e)}, "audit_id": audit_id}

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
