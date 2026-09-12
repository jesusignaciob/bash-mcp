#!/usr/bin/env node
// bash-mcp PreToolUse hook — denies bash tool calls that start with `wsl`.
// Reads JSON payload from stdin (Mavis hook protocol), writes JSON to stdout.
//
// Hook protocol:
//   stdin:  {"input": {...}, "output": {...}}
//   stdout: JSON object (merged into output), or empty for pass-through
//           _abort: {reason: "..."}  -> gates the tool call
//
// Escape hatch: if the command contains BASH_MCP_SKIP=1, the hook passes
// through (lets tests run without self-interception).

const raw = require("fs").readFileSync(0, "utf8");

let payload;
try {
  // Strip leading BOM (UTF-8) and whitespace before parsing.
  // When invoked via `wsl -d ... -- node script.js`, the harness's pipe
  // may prepend a BOM or carry over whitespace that breaks JSON.parse.
  const cleaned = raw.replace(/^\uFEFF/, "").replace(/^\s+/, "");
  payload = JSON.parse(cleaned);
} catch (e) {
  // Malformed JSON — fail open (never block the agent on our bug)
  process.stdout.write("{}");
  process.exit(0);
}

const cmd = String(((payload.input || {}).toolArgs || {}).command || "");

// Escape hatch
if (/(^|\s)BASH_MCP_SKIP=1\b/.test(cmd)) {
  process.stdout.write("{}");
  process.exit(0);
}

// Deny any wsl invocation
if (/^\s*wsl(\.exe)?\s/.test(cmd)) {
  const reason =
    "Use bash-mcp_run_command instead of `wsl -d ... -- bash -c \"...\"`. " +
    "PowerShell->WSL path has quoting, UTF-16, and PATH bugs. " +
    "See C:\\Users\\jesus\\.mavis\\skills\\bash-mcp\\SKILL.md";
  process.stdout.write(JSON.stringify({ _abort: { reason } }));
  process.exit(0);
}

// Pass through
process.stdout.write("{}");
process.exit(0);
