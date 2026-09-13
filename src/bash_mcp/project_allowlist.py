"""Per-project cwd allowlist via ``.bash-mcp.toml``.

Each project can drop a ``.bash-mcp.toml`` at the root (or any ancestor
directory) to override the global ``ALLOWED_CWD_ROOTS`` from
``bash_mcp.executor`` for ``bash_mcp_run_command`` and
``bash_mcp_session_*`` calls whose cwd lives under that directory.

Design notes:

* **Walk-up semantics.** Starting from the caller's resolved cwd, walk up
  parent directories until the first ``.bash-mcp.toml`` is found. Stop
  at ``$HOME`` (or filesystem root if ``$HOME`` is unset). Capped at
  32 hops as a safety belt — a misconfigured project can't make the
  server spend forever walking up.
* **Two modes.**

  * ``extend`` (default) — ADD the project's ``allowed_roots`` to the
    global list. Useful for projects that want to expose additional
    worktrees or build directories outside the global allowlist.
  * ``replace`` — REPLACE the global list with the project's
    ``allowed_roots``. Useful for sandboxed environments where the
    global list is too permissive.

* **Resolution rules.**

  * Relative ``allowed_roots`` entries → resolved against the TOML
    file's parent directory (``toml_dir / entry``).
  * Absolute entries → used as-is (after ``os.path.expanduser``).
  * Allowed paths may be non-existent — existence is checked separately
    in ``executor.run``.

* **Fail-soft.** Malformed TOML, unknown ``mode``, or ``allowed_roots``
  that isn't a list of strings → emit a stderr warning and return
  ``None`` (i.e. fall back to the global allowlist). The server must
  not crash because a project's TOML has a typo.

* **Process-lifetime cache.** Keyed on ``(start_dir_str, mtime_ns)``
  so editing the TOML invalidates the cached result automatically;
  restarting the server picks up TOML additions/deletions in dirs
  that were never queried before. The cache lives in the FastMCP
  worker process and dies with it.

* **Zero behavior change when no file is present.** If the walk-up
  finds nothing, ``effective_allowed_roots(start_dir)`` returns
  ``None`` and the caller falls back to ``ALLOWED_CWD_ROOTS`` exactly
  as before.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib  # noqa: F401 — stdlib in 3.11+
else:
    import tomli as tomllib  # type: ignore[import-not-found]  # noqa: F401


CONFIG_FILENAME = ".bash-mcp.toml"
MAX_WALK_UP_HOPS = 32

VALID_MODES = ("extend", "replace")
DEFAULT_MODE = "extend"


@dataclass(frozen=True)
class ProjectAllowlist:
    """A single project's resolved cwd allowlist.

    Attributes:
        config_path: Absolute path to the ``.bash-mcp.toml`` file.
        mode: ``"extend"`` or ``"replace"``.
        allowed_roots: Resolved absolute paths (post-expansion, post
            relative→absolute conversion). Empty list means "no cwd is
            allowed" — a project can use this as a deliberate lockout.
        toml_dir: Parent directory of ``config_path``. Useful for
            relative-path resolution if a future feature needs it.
    """

    config_path: str
    mode: str
    allowed_roots: tuple[str, ...] = field(default_factory=tuple)
    toml_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "config_path": self.config_path,
            "mode": self.mode,
            "allowed_roots": list(self.allowed_roots),
            "toml_dir": self.toml_dir,
        }


# Module-level cache: (start_dir_str, mtime_ns) → ProjectAllowlist | None
# mtime_ns == -1 sentinel means "no file found" — we still cache the
# negative result so we don't walk the tree on every single call.
_CACHE: dict[tuple[str, int], ProjectAllowlist | None] = {}


def _walk_up_for_allowlist(start_dir: Path) -> tuple[Path | None, int]:
    """Walk up from ``start_dir`` looking for ``.bash-mcp.toml``.

    Stops at:
      * the first directory containing ``.bash-mcp.toml`` (returns it), or
      * ``$HOME`` (returns None — don't cross the user's home), or
      * the filesystem root (returns None), or
      * ``MAX_WALK_UP_HOPS`` (returns None — safety belt).

    Returns ``(config_path, mtime_ns)``. ``mtime_ns == -1`` if no file
    was found (i.e. negative cache).
    """
    home = os.path.expanduser("~")
    cur = start_dir.resolve()
    # Anchor the walk: stop when we cross HOME upward. We compare the
    # *parents* of cur against home, because if start_dir == home the
    # user has explicitly cd'd into HOME and we still want to allow
    # HOME/.bash-mcp.toml to apply.
    for _ in range(MAX_WALK_UP_HOPS):
        candidate = cur / CONFIG_FILENAME
        if candidate.is_file():
            try:
                mtime_ns = candidate.stat().st_mtime_ns
            except OSError:
                mtime_ns = -1
            return candidate, mtime_ns
        # Stopping condition: cur is at-or-above HOME, no more walking.
        # We compare str() of the resolved path to avoid Path equality
        # edge cases (e.g. trailing slashes on different systems).
        cur_s = str(cur)
        if cur_s == home or cur_s + "/" == home + "/" or cur.parent == cur:
            return None, -1
        cur = cur.parent
    return None, -1


def _parse_toml(config_path: Path) -> ProjectAllowlist | None:
    """Parse ``config_path`` into a ``ProjectAllowlist``.

    Returns ``None`` on any parse error / invalid schema; in that case a
    warning is written to stderr and the caller falls back to the
    global allowlist.
    """
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(
            f"[bash-mcp project_allowlist] WARN: failed to parse "
            f"{config_path}: {e}. Falling back to global allowlist.",
            file=sys.stderr,
        )
        return None

    if not isinstance(data, dict):
        print(
            f"[bash-mcp project_allowlist] WARN: {config_path} root must be a "
            f"table, got {type(data).__name__}. Falling back to global.",
            file=sys.stderr,
        )
        return None

    # --- mode (default "extend") ---
    mode_raw = data.get("mode", DEFAULT_MODE)
    if not isinstance(mode_raw, str):
        print(
            f"[bash-mcp project_allowlist] WARN: {config_path} `mode` must be "
            f"a string, got {type(mode_raw).__name__}. Using default "
            f"{DEFAULT_MODE!r}.",
            file=sys.stderr,
        )
        mode = DEFAULT_MODE
    elif mode_raw not in VALID_MODES:
        print(
            f"[bash-mcp project_allowlist] WARN: {config_path} `mode` "
            f"{mode_raw!r} is not one of {VALID_MODES}. Using default "
            f"{DEFAULT_MODE!r}.",
            file=sys.stderr,
        )
        mode = DEFAULT_MODE
    else:
        mode = mode_raw

    # --- allowed_roots (required, list of strings) ---
    if "allowed_roots" not in data:
        print(
            f"[bash-mcp project_allowlist] WARN: {config_path} has no "
            f"`allowed_roots` key. Falling back to global.",
            file=sys.stderr,
        )
        return None
    raw_roots = data["allowed_roots"]
    if not isinstance(raw_roots, list):
        print(
            f"[bash-mcp project_allowlist] WARN: {config_path} "
            f"`allowed_roots` must be a list, got {type(raw_roots).__name__}. "
            f"Falling back to global.",
            file=sys.stderr,
        )
        return None
    if not all(isinstance(r, str) for r in raw_roots):
        print(
            f"[bash-mcp project_allowlist] WARN: {config_path} "
            f"`allowed_roots` must be a list of strings. Falling back to "
            f"global.",
            file=sys.stderr,
        )
        return None

    # Resolve each entry: relative → TOML dir / entry; absolute → as-is
    # (after ~ expansion). Drop empty strings.
    toml_dir = config_path.parent.resolve()
    resolved: list[str] = []
    for entry in raw_roots:
        e = entry.strip()
        if not e:
            continue
        expanded = os.path.expanduser(e)
        if os.path.isabs(expanded):
            resolved.append(expanded)
        else:
            resolved.append(str((toml_dir / expanded).resolve()))

    return ProjectAllowlist(
        config_path=str(config_path.resolve()),
        mode=mode,
        allowed_roots=tuple(resolved),
        toml_dir=str(toml_dir),
    )


def load(start_dir: str | Path) -> ProjectAllowlist | None:
    """Load (and cache) the project allowlist for ``start_dir``.

    Returns the ``ProjectAllowlist`` if a ``.bash-mcp.toml`` is found in
    or above ``start_dir`` and parses successfully. Returns ``None``
    otherwise (or on parse errors — caller should fall back to the
    global allowlist).

    The cache is keyed on ``(start_dir_str, mtime_ns)`` so editing the
    TOML invalidates the entry automatically. Negative results (no file
    found) are cached with ``mtime_ns == -1`` to avoid re-walking the
    tree on every call.
    """
    start_dir_p = Path(start_dir)
    if not start_dir_p.is_absolute():
        # Resolve relative to HOME so cache key is stable.
        start_dir_p = Path(os.path.expanduser("~")) / start_dir_p
    config_path, mtime_ns = _walk_up_for_allowlist(start_dir_p)
    cache_key = (str(start_dir_p.resolve()), mtime_ns)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    result: ProjectAllowlist | None
    if config_path is None:
        result = None
    else:
        result = _parse_toml(config_path)

    _CACHE[cache_key] = result
    return result


def effective_allowed_roots(
    start_dir: str | Path,
    global_roots: tuple[str, ...],
) -> tuple[str, ...] | None:
    """Compute the effective allowlist for a call rooted at ``start_dir``.

    Returns a tuple of absolute root paths that the call's cwd must be
    under, or ``None`` if the caller should fall back to
    ``global_roots`` unchanged (the pre-v0.8 behavior).

    Modes:
      * ``extend`` — concatenate ``global_roots`` + project's roots,
        de-duplicated while preserving order.
      * ``replace`` — return only the project's roots. (Note: an
        empty project list is a deliberate "no cwd is allowed"
        lockout — we still return ``()``, not ``None``.)
    """
    project = load(start_dir)
    if project is None:
        return None

    if project.mode == "replace":
        # Project replaces global — even an empty list is a deliberate
        # lockout. Don't fall back.
        return project.allowed_roots

    # extend — concatenate global + project, de-dupe preserving order.
    seen: set[str] = set()
    merged: list[str] = []
    for root in (*global_roots, *project.allowed_roots):
        if root not in seen:
            seen.add(root)
            merged.append(root)
    return tuple(merged)


def clear_cache() -> None:
    """Test helper: drop the module-level cache."""
    _CACHE.clear()


__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_MODE",
    "MAX_WALK_UP_HOPS",
    "ProjectAllowlist",
    "VALID_MODES",
    "clear_cache",
    "effective_allowed_roots",
    "load",
]
