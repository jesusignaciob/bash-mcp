#!/bin/bash
# ============================================================
# bash-mcp WSL Bootstrap
# Parallel to semantic-memory-launcher.sh -- does NOT edit it.
# Runs on Windows login via Scheduled Task -> wsl.exe <this script>
#
# NOTE: this WSL instance has systemd=false in /etc/wsl.conf, so
# `systemctl --user start bash-mcp.service` was a SILENT no-op
# ("Failed to connect to bus"). We launch the server in a screen session
# instead, mirroring the ExecStart of bash-mcp.service.
# ============================================================

set -uo pipefail

SCREENDIR=/tmp/screen
mkdir -p "$SCREENDIR" && chmod 700 "$SCREENDIR"
export SCREENDIR

port_up() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null || return 1
    return 0
}

if port_up 54321; then
    echo "bash-mcp already running (port 54321 open)"
    exit 0
fi

# Drop a leftover session (the server inside may be dead) and any stale socket
screen -S bash-mcp -X quit 2>/dev/null || true
screen -wipe >/dev/null 2>&1 || true

for attempt in 1 2 3; do
    echo "Starting bash-mcp in screen (attempt $attempt/3)..."
    screen -dmS bash-mcp bash -c "cd /home/jbecerra/projects/bash-mcp && source ~/.local/bin/env && export FASTMCP_SERVER_HOST=0.0.0.0 FASTMCP_SERVER_PORT=54321 && exec uv run python -m bash_mcp.server --transport streamable-http"

    for i in $(seq 1 45); do
        if port_up 54321; then
            echo "bash-mcp up on port 54321"
            exit 0
        fi
        sleep 1
    done

    echo "bash-mcp did not open port 54321; cleaning up and retrying in 15s"
    screen -S bash-mcp -X quit 2>/dev/null || true
    sleep 15
done

echo "ERROR: bash-mcp did not open port 54321" >&2
exit 1
