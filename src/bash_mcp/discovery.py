"""Binary discovery helpers (sync, thread-pool friendly).

Used by bash_list_binaries and bash_which. Kept separate from server.py
so the helpers can be unit-tested without spinning up the MCP server.
"""

from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# Tools we expect the user to call frequently.
EXPECTED_BINARIES: list[str] = [
    "rg", "fd", "bat", "eza", "broot", "jq", "sqlite3",
    "git", "docker", "node", "python3", "uv", "fnm",
    "zoxide", "starship", "curl", "wget", "ssh", "scp",
    "tar", "gzip", "make", "gcc", "ffmpeg", "vim", "nano",
    "htop", "tree", "xargs", "sed", "awk", "grep", "find",
    "trash-put", "mavis", "opencode",
]


def _resolve_one(name: str) -> dict[str, Any]:
    """Resolve a single binary. Returns {name, path, exists, version?}.

    Uses subprocess to detect version with --version / -version / -V
    (different tools use different flags). Caps at 200 chars.
    """
    path = shutil.which(name)
    out: dict[str, Any] = {"name": name, "path": path, "exists": path is not None}
    if not path:
        return out

    for flag in ("--version", "-version", "-V"):
        try:
            r = subprocess.run(
                [path, flag],
                capture_output=True, text=True, timeout=2,
            )
            blob = (r.stdout or r.stderr).strip().splitlines()
            if blob:
                out["version"] = blob[0][:200]
                break
        except (subprocess.TimeoutExpired, OSError):
            continue
    return out


def list_binaries() -> list[dict[str, Any]]:
    """Resolve all EXPECTED_BINARIES in parallel.

    Returns list sorted by: installed first, then alphabetical by name.
    """
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_resolve_one, EXPECTED_BINARIES))
    results.sort(key=lambda r: (not r["exists"], r["name"]))
    return results


def which(name: str) -> dict[str, Any]:
    """Resolve a single binary by name. Same shape as _resolve_one."""
    return _resolve_one(name)
