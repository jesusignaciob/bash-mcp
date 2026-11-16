"""Global test isolation for the bash-mcp suite.

Every test gets its own session-store database file, so the suite never
touches the real `~/.local/share/bash-mcp/sessions.db` and no test can
observe another test's persisted sessions.

The session-scoped `live_server` fixture decides whether the end-to-end
suite may run against a live bash-mcp server. It never skips silently:
a server that listens but does not answer fails the suite instead of
quietly dropping the e2e tests. See `tests/live_server.py` for the
decision matrix.
"""

from __future__ import annotations

import pytest

from tests import live_server as live_server_mod
from tests.live_server import Decision


@pytest.fixture(autouse=True)
def _isolated_session_store(tmp_path, monkeypatch):
    """Point the session store at a throwaway DB for the duration of a test."""
    monkeypatch.setenv("BASH_MCP_SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("BASH_MCP_SESSION_PERSIST", "1")
    yield


@pytest.fixture(scope="session")
def live_server():
    """Gate the live e2e suite on the state of the running server.

    RUN              -> yield the endpoint; the e2e tests execute.
    SKIP_NO_SERVER   -> skip with a reason (there is simply no server).
    SKIP_NESTED      -> skip with a reason (the suite runs inside the
                         server itself, which cannot answer while it is
                         serving this very run).
    FAIL_UNREACHABLE -> fail: the server listens but does not answer and
                         we are not inside it, so something is genuinely
                         wrong and must not be hidden behind a skip.
    """
    result = live_server_mod.probe()
    if result.decision is Decision.RUN:
        yield result.endpoint
    elif result.decision in (Decision.SKIP_NO_SERVER, Decision.SKIP_NESTED):
        pytest.skip(result.reason)
    else:
        # pytrace=False: the reason IS the diagnostic; a 40-line traceback
        # repeated for every e2e test would only bury it.
        pytest.fail(result.reason, pytrace=False)
