#!/bin/bash
# ============================================================
# bash-mcp WSL Bootstrap
# Parallel to semantic-memory-launcher.sh — does NOT edit it.
# Runs on Windows login via Scheduled Task → wsl.exe <this script>
# Ensures the bash-mcp systemd user service is started.
# ============================================================

# Start the bash-mcp service (idempotent — systemd skips if already running)
systemctl --user start bash-mcp.service 2>/dev/null

exit 0
