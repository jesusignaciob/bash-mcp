## Hard Tool Constraints — bash-mcp vs PowerShell `wsl`

For ANY command that must execute inside WSL/Ubuntu (Linux paths, Linux binaries,
python3, apt, /home/..., /mnt/c/... accessed via Linux tools, etc.):

DO NOT use the PowerShell `bash` tool with these patterns:
  - `wsl -d Ubuntu-22.04 bash -c "..."`
  - `wsl -d Ubuntu-22.04 -- bash -c "..."`
  - `wsl.exe -d Ubuntu-22.04 bash -c "..."`
  - `wsl -e ...`

A PreToolUse hook denies all of the above at the runtime layer. If the hook fires,
the response will say "Use bash-mcp_run_command instead". Do NOT retry with a
different `wsl` invocation — that pattern is permanently blocked.

ALWAYS use the bash-mcp MCP tools for WSL execution:
  - `bash-mcp_run_command({ command: "..." })` for one-shot commands
  - `bash-mcp_check_env`, `bash-mcp_list_binaries`, `bash-mcp_which`, `bash-mcp_status` for inspection
  - `bash-mcp_session_*` for stateful cwd + env across multiple calls

Escape hatch (testing only): prefix the command with `BASH_MCP_SKIP=1`. The
bash-mcp service has its own denylist (HARD + SOFT) that handles safety — you
do NOT need PowerShell's quoting/escaping layer on top.

Reserve the PowerShell `bash` tool for Windows-native commands only (`schtasks`,
`Get-ChildItem`, `reg`, `Get-ScheduledTask`, `services.msc`, etc.) — never to
invoke WSL. Reading a WSL file via `\\wsl$\` UNC path from PowerShell is fine
(it is a file share, not command execution).
