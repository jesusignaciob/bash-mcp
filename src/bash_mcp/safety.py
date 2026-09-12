"""Command safety classification.

Three classes:
- safe: execute
- dangerous: execute only if caller passes dangerous=true
- reject: always reject (hard floor; cannot be bypassed)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Class(str, Enum):
    SAFE = "safe"
    DANGEROUS = "dangerous"
    REJECT = "reject"


# Denylist review history:
#   2026-09-11 (v0.1)  — initial 9 hard + 12 soft patterns
#   2026-09-11 (v0.2)  — no changes
#   2026-09-12 (v0.3)  — no changes (concurrency-only release)
#   2026-09-12 (v0.4)  — quarterly review #1: added 6 hard + 4 soft
#                          (find -delete, find -exec rm, fork bomb variants,
#                           tee /dev/sd*, shred /etc, cat|cp|mv → /dev/sd*,
#                           rsync --delete, apt remove/purge/autoremove,
#                           pip uninstall, npm uninstall -g)


# Hard denylist — never bypassed even with dangerous=true.
# Applied to the raw command string, case-sensitive.
# Note: no trailing \b or $ — / at end-of-string is valid (rm -rf /).
# The (?!tmp|home|Users) negative lookahead exempts those prefixes.
REJECT_PATTERNS: list[str] = [
    r"\brm\s+-rf?\s+/(?!tmp|home|Users)",             # rm -rf /  (allow /tmp, /home, /Users)
    r"\bdd\s+.*\bof=/dev/(sd|hd|nvme|vd)",            # dd to raw block device
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",    # classic fork bomb: :(){ :|:& };:
    r"\bmkfs(\.[a-z0-9]+)?\s+/dev/",                  # format a device
    r">\s*/dev/(sd|hd|nvme|vd)",                     # overwrite raw block device
    r"\bchmod\s+-R\s+[0-7]{3,4}\s+/(?!tmp|home|Users)",
    r"\bcurl\s+.*\|\s*bash\b",                       # curl|sh arbitrary execution
    r"\bwget\s+.*\|\s*bash\b",
    r"\bsudo\s+rm\b",
    # === v0.4 quarterly review additions ===
    r"\bfind\b[^\n]*?\s-delete\b",                   # find ... -delete (recursive wipe)
    r"\bfind\b[^\n]*?\s-exec\s+rm\b",                # find ... -exec rm
    r":\(\)\s*\{[^{}]*\|[^{}]*&[^{}]*\}",         # fork bomb variant: any body with | and & (e.g. :(){ :|& };)
    r"\.\(\)\s*\{\s*\.\s*\|\s*\.\s*&\s*\}\s*;\s*\.",  # fork bomb variant: .(){ .|.& };. (dot function name)
    r"\w+\s*\(\s*\)\s*\{[^{}]*\|\s*\w+\s*&\s*\}",   # fork bomb variant: bomb(){ bomb|bomb& };bomb
    r"\btee\b[^\n]*?\s/dev/(sd|hd|nvme|vd)",          # tee to block device
    r"\bshred\b[^\n]*?\s(/etc/|/var/|/usr/|/boot/|/bin/|/sbin/)",  # shred in protected paths
    r"\bcat\s+/dev/(zero|urandom|random)\s*>\s*/dev/(sd|hd|nvme|vd)",  # cat → block device
    r"\bcp\s+/dev/(zero|urandom|random)\s+/dev/(sd|hd|nvme|vd)",        # cp → block device
    r"\bmv\s+[^\s]+\s+/dev/(sd|hd|nvme|vd)",                            # mv → block device
]

# Soft denylist — caller can bypass with dangerous=true.
DANGEROUS_PATTERNS: list[str] = [
    r"\bsudo\b",                                      # any sudo
    r"\bkill\s+-?9\b",                                # SIGKILL
    r"\bkill\s+-SIGKILL\b",
    r"\bsystemctl\s+(stop|disable|mask)\b",           # stop services
    r"\bgit\s+push\s+.*--force\b",                    # force push
    r"\bgit\s+push\s+-f\b",
    r"\bpip\s+install\b",                             # package install
    r"\bnpm\s+install\s+-g\b",                        # global npm install
    r"\bapt(-get)?\s+install\b",                      # system package install
    r"\bchown\s+-R\b",                                # recursive chown
    r"\bchmod\s+[0-7]{3,4}\s+/(\s|$)",                # chmod on root
    r">\s*/etc/",                                     # overwrite system config
    # === v0.4 quarterly review additions ===
    r"\brsync\b[^\n]*\s--delete\b",                   # rsync --delete (destructive)
    r"\bapt(-get)?\s+(remove|purge|autoremove)\b",    # package removal
    r"\bpip\s+uninstall\b",                           # pip uninstall
    r"\bnpm\s+(uninstall|rm|remove)\s+(-g|--global)\b",  # global npm uninstall (incl. `rm` alias and --global flag)
]


@dataclass(frozen=True)
class Classification:
    cls: Class
    matched_pattern: str | None
    pattern_index: int | None

    def to_dict(self) -> dict:
        return {
            "class": self.cls.value,
            "matched_pattern": self.matched_pattern,
            "pattern_index": self.pattern_index,
        }


def _match(patterns: list[str], command: str) -> tuple[str | None, int | None]:
    for idx, pat in enumerate(patterns):
        if re.search(pat, command):
            return pat, idx
    return None, None


def classify(command: str) -> Classification:
    """Classify a command string.

    REJECT is checked first (hard floor). DANGEROUS is checked second.
    Anything else is SAFE.
    """
    pat, idx = _match(REJECT_PATTERNS, command)
    if pat is not None:
        return Classification(Class.REJECT, pat, idx)

    pat, idx = _match(DANGEROUS_PATTERNS, command)
    if pat is not None:
        return Classification(Class.DANGEROUS, pat, idx)

    return Classification(Class.SAFE, None, None)
