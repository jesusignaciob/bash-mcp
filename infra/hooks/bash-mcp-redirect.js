#!/usr/bin/env node
// bash-mcp PreToolUse hook — denies bash tool calls that should go through bash-mcp.
// Reads JSON payload from stdin (Mavis hook protocol), writes JSON to stdout.
//
// Hook protocol:
//   stdin:  {"input": {...}, "output": {...}}
//   stdout: JSON object (merged into output), or empty for pass-through
//           _abort: {reason: "..."}  -> gates the tool call
//
// Bypass hatch: prepend BASH_MCP_SKIP=1 to the command, or include
//               a `# bash-mcp:skip` line anywhere. Genuine edge cases
//               (manual test wrappers, debugging) pass through.
//
// Scope: only fires when toolName === "bash" (matched on the .md matcher).
// This script only inspects command shape — it never executes anything.

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

// Bypass hatch — production safety valve + manual testing
if (/(^|\s)BASH_MCP_SKIP=1\b/.test(cmd) || /#\s*bash-mcp:skip\b/.test(cmd)) {
  process.stdout.write("{}");
  process.exit(0);
}

// WSL-detection patterns. Each entry: {re, why}.
// `why` is a short explanation embedded in the abort reason so the agent
// understands *why* the call was blocked, not just that it was.
const WSL_PATTERNS = [
  // 1. Direct `wsl` invocation (the only pattern in v1).
  {
    re: /^\s*wsl(\.exe)?\s/,
    why: "direct `wsl` invocation from PowerShell has quoting, UTF-16, and PATH bugs",
  },
  // 2. Direct shell exec with -c / -lc / -l (any shell, not just bash).
  {
    re: /\b(bash|sh|zsh|fish)\s+(-c|-lc|-l)\s/,
    why: "executing a shell with -c from PowerShell breaks quoting and UTF-16 boundaries",
  },
  // 3. bash/sh + WSL absolute path (the documented `bash /home/.../foo.sh` workaround gone wrong).
  {
    re: /\b(bash|sh)\s+\/(home|mnt|var|tmp|opt|root)\//,
    why: "executing a script at a WSL path from PowerShell breaks PATH resolution",
  },
  // 4. Python heredoc — PowerShell corrupts UTF-16 boundaries inside the body.
  {
    re: /\bpython[23]?\s+<<\s*['"]?[A-Z_]+/,
    why: "heredocs sent through PowerShell break at UTF-16 boundaries; write a script file and pass it via bash-mcp",
  },
  // 5. WSL-only system binaries.
  {
    re: /\b(apt|apt-get|systemctl|journalctl|service|ufw|snap|pacman|brew)\b/,
    why: "WSL-only system command — PowerShell can't reach these",
  },
  // 6. curl-pipe-bash / wget-pipe-sh — supply-chain foot-gun, broken from PowerShell.
  {
    re: /\b(curl|wget)\b[^\n]*\|\s*(bash|sh|zsh)\b/,
    why: "curl-pipe-bash from PowerShell is doubly broken (quoting AND UTF-16)",
  },
];

const matched = WSL_PATTERNS.find((p) => p.re.test(cmd));

if (matched) {
  const reason = [
    `Bash command blocked by bash-mcp enforcement: ${matched.why}.`,
    ``,
    `Use the bash-mcp MCP tool instead. It runs inside WSL with proper quoting, UTF-8, and PATH.`,
    `Actual server tool names (no bash-mcp_ prefix on the core four):`,
    `  - bash_run_command({command: "<your command>"})`,
    `  - bash_run_command({command: "<your command>", cwd: "/home/jbecerra/<dir>"})`,
    `  - bash_run_command({command: "<your command>", dangerous: true})   // SOFT denylist bypass`,
    `  - bash_run_command({command: "<script content>", timeout_ms: 600000})`,
    `  - bash_check_env()                          // OS / PATH / Python / cwd`,
    `  - bash_list_binaries() / bash_which({name})  // tool discovery`,
    `  - bash_mcp_classify({command})                  // pre-flight denylist check`,
    ``,
    `Need a one-off bypass? Prepend BASH_MCP_SKIP=1 to the command, or add a \`# bash-mcp:skip\` line.`,
    `See C:\\Users\\jesus\\.mavis\\skills\\bash-mcp\\SKILL.md for the full tool reference.`,
  ].join("\n");
  process.stdout.write(JSON.stringify({ _abort: { reason } }));
  process.exit(0);
}

// Pass through — pure Windows commands, git/npm/docker on Windows, etc.
process.stdout.write("{}");
process.exit(0);
