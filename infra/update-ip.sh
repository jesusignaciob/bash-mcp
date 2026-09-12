#!/bin/bash
# ============================================================
# Actualiza la IP de WSL para el entry bash-mcp en mcp.json
# Parallel to semantic-memory/update-ip.sh — does NOT edit it.
# Use this after a WSL reboot to keep bash-mcp URL fresh.
# ============================================================

set -e

WSL_IP=$(hostname -I | awk '{print $1}')
echo "WSL IP: $WSL_IP"

# Actualizar MiniMax Code MCP config (solo entry bash-mcp)
MINIMAX_MCP="/mnt/c/Users/jesus/.minimax/mcp/mcp.json"
if [ -f "$MINIMAX_MCP" ]; then
    sed -i "s|http://[0-9.]*:54321/mcp/|http://$WSL_IP:54321/mcp/|g" "$MINIMAX_MCP"
    echo "Updated: $MINIMAX_MCP (bash-mcp only)"
fi

# Actualizar Cursor MCP config (si existe, solo bash-mcp)
CURSOR_MCP="/mnt/c/Users/jesus/.cursor/mcp.json"
if [ -f "$CURSOR_MCP" ]; then
    sed -i "s|http://[0-9.]*:54321/mcp/|http://$WSL_IP:54321/mcp/|g" "$CURSOR_MCP"
    echo "Updated: $CURSOR_MCP (bash-mcp only)"
fi

echo "Done. bash-mcp accessible at: http://$WSL_IP:54321/mcp/"
