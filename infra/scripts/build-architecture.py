#!/usr/bin/env python3
"""Render `architecture.html` from `architecture.mermaid`.

The `.mermaid` file is the source of truth for the architecture diagram.
This script embeds it in a standalone HTML page, so the two can never
drift apart.

Usage:
    uv run python infra/scripts/build-architecture.py
    uv run python infra/scripts/build-architecture.py --check   # CI: fail if stale
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MERMAID_PATH = REPO_ROOT / "architecture.mermaid"
HTML_PATH = REPO_ROOT / "architecture.html"


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>bash-mcp — architecture</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff;
    --fg: #1a1a1a;
    --muted: #64748b;
    --card: #f6f8fa;
    --border: #e2e5e9;
    --accent: #3b6fd4;
    --accent-soft: #eaf0fd;
    --warn: #d97706;
    --warn-soft: #fdf6e8;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115;
      --fg: #e6e6e6;
      --muted: #9aa0a6;
      --card: #171a21;
      --border: #2a2f3a;
      --accent: #6f9dff;
      --accent-soft: #1b2436;
      --warn: #e0a44a;
      --warn-soft: #2a2318;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--fg);
    line-height: 1.5;
  }
  header {
    padding: 28px 24px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 12px;
  }
  header .eyebrow {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
  }
  header h1 { margin: 0; font-size: 22px; font-weight: 650; letter-spacing: -0.01em; }
  header .tag {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    color: var(--muted);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 2px 10px;
  }
  main { padding: 24px; max-width: 1080px; }
  .diagram {
    background: #ffffff;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    overflow-x: auto;
  }
  .diagram .mermaid, .diagram svg { display: flex; justify-content: center; }
  .diagram svg { max-width: 100%; height: auto; }
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 18px;
    margin: 16px 0 0;
    font-size: 12px;
    color: var(--muted);
  }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .swatch { width: 12px; height: 12px; border-radius: 3px; border: 1px solid; display: inline-block; }
  .facts {
    display: grid;
    grid-template-columns: 1.1fr 1fr;
    gap: 16px;
    margin: 24px 0 0;
  }
  @media (max-width: 720px) { .facts { grid-template-columns: 1fr; } }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 18px;
  }
  .card h2 {
    margin: 0 0 8px;
    font-size: 13px;
    font-weight: 650;
    letter-spacing: 0.02em;
  }
  .card ul { margin: 0; padding-left: 18px; font-size: 13px; }
  .card li { margin: 3px 0; }
  .card code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    background: rgba(45, 49, 66, 0.06);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .bar { display: flex; gap: 8px; margin: 20px 0 0; }
  button {
    font: inherit; font-size: 13px; padding: 7px 13px;
    border-radius: 6px; border: 1px solid var(--border);
    background: var(--card); color: var(--fg); cursor: pointer;
  }
  button:hover { border-color: var(--accent); }
  details { margin-top: 16px; }
  summary { cursor: pointer; color: var(--muted); font-size: 13px; }
  pre.src {
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 14px; overflow: auto; font-size: 12.5px; line-height: 1.5;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  footer {
    padding: 18px 24px; color: var(--muted); font-size: 12px;
    border-top: 1px solid var(--border); margin-top: 28px;
  }
  footer code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    background: rgba(45, 49, 66, 0.06); padding: 1px 4px; border-radius: 3px;
  }
</style>
</head>
<body>
<header>
  <span class="eyebrow">bash-mcp</span>
  <h1>Architecture</h1>
  <span class="tag">v0.9.0 · mermaid source of truth</span>
</header>
<main>
  <div class="diagram">
    <pre class="mermaid">__MERMAID_HTML__</pre>
  </div>

  <div class="legend">
    <span><i class="swatch" style="background:#eaf0fd;border-color:#3b6fd4"></i> core (server tools)</span>
    <span><i class="swatch" style="background:#fdf6e8;border-color:#d97706"></i> guardrails (denylist, allowlist)</span>
    <span><i class="swatch" style="background:#ffffff;border-color:#94a3b8"></i> module</span>
    <span><i class="swatch" style="background:#f1f5f9;border-color:#94a3b8"></i> on-disk state</span>
    <span><i class="swatch" style="background:#f8fafc;border-color:#cbd5e1"></i> optional / not registered</span>
  </div>

  <div class="facts">
    <div class="card">
      <h2>The request path</h2>
      <ul>
        <li>The MCP client posts JSON-RPC over <code>streamable-http</code> to <code>:54321/mcp/</code>.</li>
        <li><code>safety.py</code> classifies the command first: HARD rejects cannot be overridden, SOFT ones need <code>dangerous=true</code>.</li>
        <li><code>project_allowlist.py</code> resolves the cwd allowlist (global + the nearest <code>.bash-mcp.toml</code>).</li>
        <li><code>executor.py</code> spawns <code>bash -lc</code> under the concurrency semaphore, with a timeout and an output cap.</li>
        <li>Every call is appended to <code>audit.jsonl</code>.</li>
      </ul>
    </div>
    <div class="card">
      <h2>What is stateful</h2>
      <ul>
        <li><code>sessions.py</code> keeps a <code>cwd</code> + <code>env</code> per session.</li>
        <li>Since v0.9 that state is mirrored to <code>sessions.db</code> (SQLite) and restored on boot.</li>
        <li>Opt out with <code>BASH_MCP_SESSION_PERSIST=0</code>.</li>
        <li>The PreToolUse hook blocks PowerShell shapes that break against WSL (quoting, UTF-16).</li>
        <li><code>launcher.sh</code> starts the server in a <code>screen</code> session — this WSL runs <code>systemd=false</code>.</li>
      </ul>
    </div>
  </div>

  <div class="bar">
    <button id="copy">Copy Mermaid source</button>
    <button id="download">Download .mermaid</button>
  </div>

  <details>
    <summary>View Mermaid source</summary>
    <pre class="src">__MERMAID_HTML__</pre>
  </details>
</main>
<footer>
  Generated from <code>architecture.mermaid</code> — regenerate with
  <code>uv run python infra/scripts/build-architecture.py</code>.
  The <code>.mermaid</code> file is the source of truth; this page is its render.
</footer>
<script>
  const SRC = __MERMAID_JSON__;
  document.getElementById("copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(SRC); } catch (e) {}
  });
  document.getElementById("download").addEventListener("click", () => {
    const blob = new Blob([SRC], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "architecture.mermaid";
    a.click();
    URL.revokeObjectURL(a.href);
  });
</script>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({
    startOnLoad: true,
    theme: "base",
    securityLevel: "strict",
    themeVariables: {
      background: "#ffffff",
      primaryColor: "#ffffff",
      primaryTextColor: "#1f2937",
      primaryBorderColor: "#94a3b8",
      lineColor: "#64748b",
      secondaryColor: "#f7f9fc",
      tertiaryColor: "#fbfcfd",
      fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
      fontSize: "13px"
    }
  });
</script>
</body>
</html>
"""


def build(source: str) -> str:
    """Return the HTML page for the given mermaid source."""
    escaped = html.escape(source, quote=False)
    return TEMPLATE.replace("__MERMAID_HTML__", escaped).replace(
        "__MERMAID_JSON__", json.dumps(source)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if architecture.html is out of date (CI mode)",
    )
    args = parser.parse_args()

    if not MERMAID_PATH.exists():
        print(f"ERROR: {MERMAID_PATH} not found", file=sys.stderr)
        return 1

    source = MERMAID_PATH.read_text(encoding="utf-8")
    page = build(source)

    if args.check:
        current = HTML_PATH.read_text(encoding="utf-8") if HTML_PATH.exists() else ""
        if current != page:
            print(
                "ERROR: architecture.html is stale — run: "
                "uv run python infra/scripts/build-architecture.py",
                file=sys.stderr,
            )
            return 1
        print("architecture.html is up to date")
        return 0

    HTML_PATH.write_text(page, encoding="utf-8", newline="\n")
    print(
        f"wrote {HTML_PATH.relative_to(REPO_ROOT)} "
        f"({len(page)} bytes) from {MERMAID_PATH.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
