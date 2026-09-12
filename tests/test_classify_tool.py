"""Unit tests for the bash_mcp_classify explainer tool (v0.7).

Same pattern as test_sessions.py: use the FastMCP FunctionTool.fn
accessor to call the tool directly without an MCP client.
"""

from __future__ import annotations

import pytest

from bash_mcp import server
from bash_mcp.safety import REJECT_PATTERNS, DANGEROUS_PATTERNS


def _call(name: str):
    """Dig out the underlying callable for a FastMCP-registered tool."""
    tool = server.mcp._tool_manager._tools[name]
    return tool.fn


_classify = _call("bash_mcp_classify")


# --- class routing ---

def test_classify_safe_command_returns_safe_class():
    out = _classify("echo hi")
    assert out["class"] == "safe"
    assert out["matched_pattern"] is None
    assert out["pattern_index"] is None


def test_classify_dangerous_returns_dangerous_class():
    out = _classify("sudo apt update")
    assert out["class"] == "dangerous"
    assert out["matched_pattern"] is not None
    assert out["pattern_index"] is not None


def test_classify_reject_returns_reject_class():
    out = _classify("rm -rf /")
    assert out["class"] == "reject"
    assert out["matched_pattern"] is not None
    assert out["pattern_index"] is not None


# --- matched_pattern / pattern_index ---

def test_classify_returns_matched_pattern_for_dangerous():
    """Exact regex string for a known dangerous pattern."""
    out = _classify("sudo apt update")
    assert out["matched_pattern"] == DANGEROUS_PATTERNS[out["pattern_index"]]
    assert "\\bsudo\\b" in out["matched_pattern"]


def test_classify_returns_matched_pattern_for_reject():
    """Exact regex string for the canonical reject pattern (rm -rf /)."""
    out = _classify("rm -rf /")
    assert out["matched_pattern"] == REJECT_PATTERNS[out["pattern_index"]]
    assert "rm" in out["matched_pattern"]


def test_classify_pattern_index_is_integer_for_matches():
    out = _classify("sudo echo hi")
    assert isinstance(out["pattern_index"], int)
    assert out["pattern_index"] >= 0


def test_classify_pattern_index_is_none_for_safe():
    out = _classify("echo hi")
    assert out["pattern_index"] is None


# --- hints ---

def test_classify_hint_with_dangerous_false_differs_for_dangerous_class():
    """For dangerous, hint_with_dangerous_false mentions the override option."""
    out = _classify("sudo echo hi")
    # Both hints are present and non-empty.
    assert out["hint_with_dangerous_false"]
    assert out["hint_with_dangerous_true"]
    # The "false" hint mentions the override option (which is the actionable
    # thing the agent must do if it wants to proceed).
    assert "dangerous=true" in out["hint_with_dangerous_false"]


def test_classify_hints_identical_for_safe_command():
    out = _classify("echo hi")
    assert out["hint_with_dangerous_false"] == out["hint_with_dangerous_true"]


# --- would_execute flags ---

def test_classify_would_execute_true_for_safe():
    out = _classify("echo hi")
    assert out["would_execute"] is True
    assert out["would_execute_with_dangerous_true"] is True


def test_classify_would_execute_false_for_reject_even_with_dangerous():
    out = _classify("rm -rf /")
    assert out["would_execute"] is False
    assert out["would_execute_with_dangerous_true"] is False  # HARD cannot be bypassed


def test_classify_would_execute_true_for_dangerous_with_dangerous_true():
    out = _classify("sudo echo hi")
    assert out["would_execute"] is False
    assert out["would_execute_with_dangerous_true"] is True


# --- audit + side-effect guarantees ---

def test_classify_does_not_execute_command():
    """The whole point of classify: confirm a dangerous command does NOT run.

    Pass a command that would create a marker file. After the call, verify
    the file does not exist.
    """
    import os
    import tempfile
    marker = os.path.join(tempfile.gettempdir(), "bash-mcp-classify-pwned-marker")
    # Ensure clean state.
    if os.path.exists(marker):
        os.unlink(marker)
    out = _classify(f"touch {marker}")
    assert out["class"] in {"safe", "dangerous", "reject"}
    assert not os.path.exists(marker), (
        f"classify must not execute, but marker {marker} was created"
    )


def test_classify_audits_each_call():
    out = _classify("echo classified-audit-test")
    assert "audit_id" in out
    assert isinstance(out["audit_id"], str)
    assert len(out["audit_id"]) > 0


def test_classify_empty_command_is_safe():
    """Empty string doesn't match any pattern (REJECT and DANGEROUS
    patterns require at least one non-whitespace character)."""
    out = _classify("")
    assert out["class"] == "safe"
    assert out["would_execute"] is True
