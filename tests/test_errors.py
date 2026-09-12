"""Unit tests for the error envelope shape (server.make_error_response + safety errors)."""

from __future__ import annotations

import pytest

from bash_mcp.safety import Class, classify
from bash_mcp.server import make_error_response


# --- make_error_response shape ---

def test_error_envelope_has_required_fields() -> None:
    err = make_error_response(
        code="FORBIDDEN_COMMAND",
        message="msg",
        hint="hint",
    )
    assert "error" in err
    e = err["error"]
    assert e["code"] == "FORBIDDEN_COMMAND"
    assert e["message"] == "msg"
    assert e["hint"] == "hint"
    assert "documentation" in e


def test_error_envelope_with_matched_pattern() -> None:
    err = make_error_response(
        code="FORBIDDEN_COMMAND",
        message="m",
        hint="h",
        matched_pattern=r"\brm\s+-rf?",
    )
    assert err["error"]["matched_pattern"] == r"\brm\s+-rf?"


def test_error_envelope_with_audit_id_and_classification() -> None:
    err = make_error_response(
        code="X", message="m", hint="h",
        audit_id="abc123",
        classification={"class": "reject", "matched_pattern": None, "pattern_index": None},
    )
    assert err["audit_id"] == "abc123"
    assert err["classification"]["class"] == "reject"


# --- Safety error wiring ---

def test_forbidden_command_classification() -> None:
    cls = classify("rm -rf /etc")
    assert cls.cls == Class.REJECT
    assert cls.matched_pattern is not None


def test_dangerous_command_classification() -> None:
    cls = classify("sudo apt install vim")
    assert cls.cls == Class.DANGEROUS
    assert cls.matched_pattern is not None


def test_safe_command_classification() -> None:
    cls = classify("echo hello")
    assert cls.cls == Class.SAFE
    assert cls.matched_pattern is None


def test_forbidden_command_hint_mentions_no_bypass() -> None:
    """The FORBIDDEN_COMMAND hint must mention dangerous=true is no bypass."""
    cls = classify("rm -rf /etc")
    err = make_error_response(
        code="FORBIDDEN_COMMAND",
        message="Command matches the hard denylist and cannot be executed.",
        hint="This pattern cannot be bypassed even with dangerous=true. Modify the command.",
        matched_pattern=cls.matched_pattern,
        audit_id="test",
        classification=cls.to_dict(),
    )
    assert "dangerous=true" in err["error"]["hint"]
    assert "bypass" in err["error"]["hint"]


def test_dangerous_command_hint_suggests_override() -> None:
    """The DANGEROUS_COMMAND_REQUIRES_OVERRIDE hint must suggest the override."""
    cls = classify("sudo apt install vim")
    err = make_error_response(
        code="DANGEROUS_COMMAND_REQUIRES_OVERRIDE",
        message="Command matches the soft denylist and was not authorized.",
        hint="Retry with dangerous=true if intentional, OR remove the dangerous fragment.",
        matched_pattern=cls.matched_pattern,
        audit_id="test",
        classification=cls.to_dict(),
    )
    assert "dangerous=true" in err["error"]["hint"]
    assert "remove" in err["error"]["hint"].lower()


def test_invalid_cwd_hint_mentions_allowlist() -> None:
    """The INVALID_CWD hint must mention the allowlist roots."""
    err = make_error_response(
        code="INVALID_CWD",
        message="cwd not under an allowed root: /etc",
        hint="cwd '/etc' is not under an allowed root. Allowed roots: /home/jbecerra, /tmp, /home, /mnt/c/Users/jesus, /var/tmp.",
        audit_id="test",
    )
    hint_lower = err["error"]["hint"].lower()
    assert "allowed roots" in hint_lower
    assert "/tmp" in err["error"]["hint"]
    assert "/mnt/c/Users/jesus" in err["error"]["hint"]
