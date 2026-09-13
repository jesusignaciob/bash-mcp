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

from bash_mcp import __version__, audit, sessions
from bash_mcp.sessions import (
    MAX_SESSION_NAME_LEN,
    active_count,
    apply_state_update,
    create as session_create,
    destroy as session_destroy,
    get as session_get,
    list_active as session_list_active,
    snapshot_for_run,
)
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
from bash_mcp.safety import Class, Classification, classify  # classify is also exposed as a tool

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
    """Hint surfaced on InvalidCwdError.

    v0.8: when a project allowlist is in effect (a ``.bash-mcp.toml``
    lives in or above ``cwd_attempted``), mention it so the user knows
    the source of the (possibly different) allowlist.
    """
    base = (
        f"cwd '{cwd_attempted}' is not under an allowed root. "
    )
    try:
        from bash_mcp import project_allowlist  # local import: server hot path
        proj = project_allowlist.load(cwd_attempted)
    except Exception:
        proj = None
    if proj is not None:
        base += (
            f"Project allowlist ({proj.config_path}, mode={proj.mode!r}) "
            f"allowed roots: {', '.join(proj.allowed_roots) or '<none — lockout>'}. "
        )
    else:
        base += (
            f"Allowed roots: {', '.join(ALLOWED_CWD_ROOTS)}. "
        )
    base += (
        "If the path is a Windows-style path (C:\\foo), it is automatically converted "
        "to /mnt/c/foo. Otherwise, pass a path under one of the allowed roots."
    )
    return base


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
            "gzip_threshold_bytes": audit.GZIP_THRESHOLD_BYTES,
            "gzip_enabled": audit.GZIP_THRESHOLD_BYTES > 0,
        },
        "concurrency": {
            "max_concurrent": MAX_CONCURRENT,
            "active": concurrency_active(),
        },
        "sessions": {
            "active": active_count(),
            "max_env_per_session": sessions.MAX_ENV_VARS,
            "idle_timeout_s": sessions.IDLE_TIMEOUT_S,
            "janitor_enabled": sessions.IDLE_TIMEOUT_S > 0,
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
            "bash_mcp_classify",
            "bash_mcp_audit_read",
            "bash_mcp_session_create",
            "bash_mcp_session_run",
            "bash_mcp_session_destroy",
            "bash_mcp_session_list",
            "echo",
        ],
    }



# --- v0.7: denylist explainer ---


def _hint_for_class(cls_value: str, dangerous: bool) -> str:
    """Build the explainer hint for a classification + dangerous flag.

    Mirrors the strings used in bash_run_command / bash_mcp_session_run
    so the explainer matches what would actually happen.
    """
    if cls_value == Class.SAFE.value:
        return "Command would execute without override."
    if cls_value == Class.REJECT.value:
        return (
            "This pattern cannot be bypassed even with dangerous=true. "
            "Modify the command to avoid the dangerous fragment "
            "(e.g. use /tmp or /home instead of / for rm targets; "
            "do not pipe curl/wget directly into bash)."
        )
    if cls_value == Class.DANGEROUS.value:
        return (
            "Retry bash_run_command with dangerous=true if this is intentional, "
            "OR remove the dangerous fragment (e.g. drop sudo, use --no-force, "
            "drop kill -9)."
        )
    return ""


@mcp.tool
def bash_mcp_classify(command: str) -> dict[str, Any]:
    """Classify a command string against the bash-mcp denylist.

    Read-only. Does NOT execute. Use this BEFORE bash_run_command if you're
    unsure whether a command will be rejected. Saves a round-trip and gives
    you the exact rule that would fire.

    Args:
        command: The bash command line to classify.

    Returns:
        {class, matched_pattern, pattern_index,
         hint_with_dangerous_false, hint_with_dangerous_true,
         would_execute, would_execute_with_dangerous_true, audit_id}

        * class            in {"safe", "dangerous", "reject"}
        * matched_pattern  = the regex string that fired (null if safe)
        * pattern_index    = 0-based index in REJECT/DANGEROUS list (null if safe)
        * hint_with_dangerous_false / ..._true
                          = actionable next-step strings (one per scenario)
        * would_execute    = true iff class == "safe"
        * would_execute_with_dangerous_true
                          = true iff class != "reject" (i.e. dangerous still
                            rejected without override, but accepted WITH override)
        * audit_id         = id of the audit entry written for this call
    """
    audit_id = audit.new_audit_id()
    cls: Classification = classify(command)
    audit.log({
        "ts": audit_id.split("-")[0] if "-" in audit_id else "",
        "audit_id": audit_id,
        "tool": "bash_mcp_classify",
        "args": {"command": command, "command_len": len(command)},
        "classification": cls.to_dict(),
        "outcome": "CLASSIFIED",
    })
    return {
        "class": cls.cls.value,
        "matched_pattern": cls.matched_pattern,
        "pattern_index": cls.pattern_index,
        "hint_with_dangerous_false": _hint_for_class(cls.cls.value, dangerous=False),
        "hint_with_dangerous_true":  _hint_for_class(cls.cls.value, dangerous=True),
        "would_execute": cls.cls == Class.SAFE,
        "would_execute_with_dangerous_true": cls.cls != Class.REJECT,
        "audit_id": audit_id,
    }


# --- v0.6: stateful sessions ---


@mcp.tool
def bash_mcp_session_create(
    name: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Create a new stateful session.

    Sessions persist `cwd` and exported `env` across calls within the
    server process. State is LOST on server restart (v0.6 limitation —
    not persisted to disk). Caller is responsible for `destroy`-ing
    sessions when done; idle sessions are not auto-cleaned.

    Only top-level `cd` and `export`/`unset` statements on simple
    command chains are tracked. Shell scripts, functions, conditionals,
    `$(...)`, backticks, heredocs, multi-line `\\` continuations are
    out of scope.

    Args:
        name: Optional human-readable label (≤64 chars). Defaults to
            `session-<first 8 of id>`.
        cwd: Optional starting working directory. Must be under an
            allowed root (same rules as `bash_run_command`). Defaults
            to `$HOME`.

    Returns:
        {session_id, name, cwd, created_at, last_used_at, env_count=0}
        On invalid cwd: {error: {code: INVALID_CWD, ...}}.
        On bad name:   {error: {code: INVALID_ARGUMENT, ...}}.
    """
    audit_id = audit.new_audit_id()
    try:
        state = session_create(name=name, cwd=cwd)
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
            hint=f"name must be a string ≤{MAX_SESSION_NAME_LEN} chars.",
            audit_id=audit_id,
        )

    audit.log({
        "ts": audit_id.split("-")[0] if "-" in audit_id else "",
        "audit_id": audit_id,
        "tool": "bash_mcp_session_create",
        "args": {"name": state.name, "cwd": state.cwd},
        "outcome": "CREATED",
        "session_id": state.session_id,
    })

    return {
        "session_id": state.session_id,
        "name": state.name,
        "cwd": state.cwd,
        "created_at": state.created_at,
        "last_used_at": state.last_used_at,
        "env_count": len(state.env),
        "audit_id": audit_id,
    }


@mcp.tool
def bash_mcp_session_run(
    session_id: str,
    command: str,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    dangerous: bool = False,
) -> dict[str, Any]:
    """Run a command in a session, persisting cwd and env changes.

    Same semantics as `bash_run_command` for:
      * denylist (HARD always rejected, SOFT needs `dangerous=true`)
      * concurrency limit (sessions share the global
        `BASH_MCP_MAX_CONCURRENT`)
      * timeout / output truncation / audit

    Adds:
      * runs in `session.cwd` (instead of `$HOME` default)
      * inherits `session.env` (after `os.environ`)
      * parses top-level `cd PATH`, `export VAR=value`, `unset VAR` and
        updates the session after `exit_code == 0`.
      * returns `cwd` and `env_keys` in the response so callers can
        confirm state.

    Returns:
        {stdout, stderr, exit_code, duration_ms, timed_out, truncated,
         classification, audit_id, session_id, cwd, env_keys}
        On missing session: {error: {code: SESSION_NOT_FOUND, ...}}.
    """
    audit_id = audit.new_audit_id()

    # Step 1: snapshot session cwd + env (under lock, then release).
    session_cwd, session_env = snapshot_for_run(session_id)
    if session_env is None:
        return make_error_response(
            code="SESSION_NOT_FOUND",
            message=f"No active session with id '{session_id}'.",
            hint=(
                "Create one with bash_mcp_session_create, or list active "
                "sessions with bash_mcp_session_list. Note that sessions "
                "are process-lifetime — a server restart wipes them."
            ),
            audit_id=audit_id,
        )

    # Step 2: classify (same rules as bash_run_command).
    cls: Classification = classify(command)
    if cls.cls == Class.REJECT:
        audit.log({
            "ts": audit_id.split("-")[0] if "-" in audit_id else "",
            "audit_id": audit_id,
            "tool": "bash_mcp_session_run",
            "args": {
                "session_id": session_id,
                "command": command,
                "timeout_ms": timeout_ms,
                "dangerous": dangerous,
            },
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
            "tool": "bash_mcp_session_run",
            "args": {
                "session_id": session_id,
                "command": command,
                "timeout_ms": timeout_ms,
                "dangerous": dangerous,
            },
            "classification": cls.to_dict(),
            "outcome": "DENIED",
            "reason": "matched soft denylist; dangerous flag not set",
        })
        return make_error_response(
            code="DANGEROUS_COMMAND_REQUIRES_OVERRIDE",
            message="Command matches the soft denylist and was not authorized.",
            hint=(
                "Retry with dangerous=true if this is intentional, OR remove "
                "the dangerous fragment (e.g. drop sudo, use --no-force, "
                "drop kill -9)."
            ),
            matched_pattern=cls.matched_pattern,
            audit_id=audit_id,
            classification=cls.to_dict(),
        )

    # Step 3: build env — inherit process env + session env + caller env
    # (caller wins). Same shape as bash_run_command.
    full_env = os.environ.copy()
    if session_env:
        for k, v in session_env.items():
            full_env[k] = v

    # Step 4: execute via executor.run (which already wraps concurrency.slot()).
    try:
        result: ExecutionResult = exec_run(
            command=command,
            cwd=session_cwd,
            timeout_ms=timeout_ms,
            env=full_env,
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
            hint=_hint_for_invalid_cwd(session_cwd),
            audit_id=audit_id,
        )
    except ValueError as e:
        return make_error_response(
            code="INVALID_ARGUMENT",
            message=str(e),
            hint="Check timeout_ms range (1..600000) and that command is non-empty.",
            audit_id=audit_id,
        )

    # Step 5: re-snapshot to know if session still exists, then apply cd/export.
    latest = session_get(session_id)
    if latest is not None:
        apply_state_update(session_id, command, result.exit_code)
        # Re-read after apply for the response (cheap).
        latest = session_get(session_id)

    # Step 6: audit (same shape as bash_run_command + session_id).
    audit.log_command(
        tool="bash_mcp_session_run",
        args={
            "session_id": session_id,
            "command": command,
            "cwd": session_cwd,
            "timeout_ms": timeout_ms,
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

    # Step 7: return
    payload = result.to_dict()
    payload["classification"] = cls.to_dict()
    payload["audit_id"] = audit_id
    payload["session_id"] = session_id
    if latest is not None:
        payload["cwd"] = latest.cwd
        payload["env_keys"] = sorted(latest.env.keys())
    else:
        # Destroyed during the run — best-effort fallback.
        payload["cwd"] = session_cwd
        payload["env_keys"] = sorted(session_env.keys()) if session_env else []
    if dangerous and cls.cls == Class.DANGEROUS:
        payload["warning"] = "Executed with dangerous=true override"
    return payload


@mcp.tool
def bash_mcp_session_destroy(session_id: str) -> dict[str, Any]:
    """Destroy a session and free its memory.

    Returns:
        {ok: true, session_id, freed_env_vars}
        On missing session: {error: {code: SESSION_NOT_FOUND, ...}}.
    """
    audit_id = audit.new_audit_id()
    # Check existence BEFORE destroy; otherwise a real session with
    # empty env would be indistinguishable from "never existed".
    pre = session_get(session_id)
    if pre is None:
        return make_error_response(
            code="SESSION_NOT_FOUND",
            message=f"No active session with id '{session_id}'.",
            hint=(
                "Check bash_mcp_session_list for active session ids. "
                "Note that sessions are process-lifetime."
            ),
            audit_id=audit_id,
        )
    freed = session_destroy(session_id)

    audit.log({
        "ts": audit_id.split("-")[0] if "-" in audit_id else "",
        "audit_id": audit_id,
        "tool": "bash_mcp_session_destroy",
        "args": {"session_id": session_id},
        "outcome": "DESTROYED",
        "freed_env_vars": freed,
    })

    return {
        "ok": True,
        "session_id": session_id,
        "freed_env_vars": freed,
        "audit_id": audit_id,
    }


@mcp.tool
def bash_mcp_session_list() -> dict[str, Any]:
    """List active sessions (id, name, cwd, last_used_at, env_count).

    Returns:
        {sessions: [...], count: N} — array of session summaries,
        most-recently-used first. Wrapped in an object (not a bare list)
        so clients get a stable shape even when there is exactly one
        session: some MCP transports unwrap single-element list responses.
    """
    items = session_list_active()
    return {"sessions": items, "count": len(items)}


@mcp.tool
def bash_mcp_audit_read(
    backup_index: int = 0,
    max_entries: int = 1000,
    tool_filter: str | None = None,
) -> dict[str, Any]:
    """Read audit log entries from a backup.

    Closes the v0.7 gzip one-way archival caveat: transparently decompresses
    `audit.jsonl.N.gz` backups in addition to plaintext `audit.jsonl.N`.

    Args:
        backup_index: 0 = current `audit.jsonl`; 1..N = rotated backup N
            (most recent is 1, oldest kept is `BACKUP_COUNT`).
        max_entries: hard cap on returned list (default 1000). Read stops
            after this many matching entries.
        tool_filter: optional tool name to filter by (e.g. "bash_run_command",
            "bash_mcp_classify"). Exact match, case-sensitive.

    Returns:
        {backup_index, path, format ("plaintext" | "gzip"),
         total_in_file, returned, entries: [...]}

        On non-existent backup_index: {error: {code: "BACKUP_NOT_FOUND", ...}}

    Audited as `tool="bash_mcp_audit_read" outcome="READ"`.
    """
    audit_id = audit.new_audit_id()

    path = audit.read_backup_path(backup_index)
    if path is None:
        return make_error_response(
            code="BACKUP_NOT_FOUND",
            message=f"No audit backup with index {backup_index}.",
            hint=(
                f"backup_index must be 0 (current) or 1..{audit.BACKUP_COUNT}. "
                "Check bash_mcp_status().audit.backups_present for the list of existing backups."
            ),
            audit_id=audit_id,
        )

    entries = audit.read_backup(
        backup_index, max_entries=max_entries, tool_filter=tool_filter
    )
    fmt = "gzip" if path.suffix == ".gz" else "plaintext"
    audit.log({
        "ts": audit_id.split("-")[0] if "-" in audit_id else "",
        "audit_id": audit_id,
        "tool": "bash_mcp_audit_read",
        "args": {
            "backup_index": backup_index,
            "max_entries": max_entries,
            "tool_filter": tool_filter,
        },
        "outcome": "READ",
        "format": fmt,
        "returned": len(entries),
    })
    return {
        "backup_index": backup_index,
        "path": str(path),
        "format": fmt,
        "total_in_file": len(entries),  # capped at max_entries
        "returned": len(entries),
        "entries": entries,
        "audit_id": audit_id,
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
