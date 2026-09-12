#!/bin/bash
# ============================================================
# bash-mcp installer — idempotent.
# Run from the repo root: ./infra/install.sh
#
# Deploys:
#   - systemd unit       → ~/.config/systemd/user/bash-mcp.service
#   - PreToolUse hook     → /mnt/c/Users/jesus/.minimax/agents/mavis/hooks/bash-mcp-redirect.md
#   - Skill               → /mnt/c/Users/jesus/.mavis/skills/bash-mcp/{SKILL.md,_meta.json}
#   - mcp.json entry      → /mnt/c/Users/jesus/.minimax/mcp/mcp.json (idempotent overwrite of bash-mcp)
#
# Prints the manual step (mavis mcp create) for Windows PowerShell.
# ============================================================
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$REPO_ROOT/infra"
WSL_USER_HOME="/mnt/c/Users/jesus"

echo "=== bash-mcp install ==="
echo "Repo root: $REPO_ROOT"
echo ""

# 1. systemd unit
echo "[1/4] systemd unit"
mkdir -p ~/.config/systemd/user
cp -f "$INFRA/systemd/bash-mcp.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable bash-mcp.service 2>/dev/null
systemctl --user restart bash-mcp.service
echo "  -> enabled + restarted"

# 2. PreToolUse hook
echo ""
echo "[2/4] PreToolUse hook"
HOOK_DST="$WSL_USER_HOME/.minimax/agents/mavis/hooks"
mkdir -p "$HOOK_DST"
cp -f "$INFRA/hooks/bash-mcp-redirect.md" "$HOOK_DST/"
cp -f "$INFRA/hooks/bash-mcp-redirect.js" "$HOOK_DST/"
echo "  -> copied to $HOOK_DST/"

# 3. Skill
echo ""
echo "[3/4] Skill"
SKILL_DST="$WSL_USER_HOME/.mavis/skills/bash-mcp"
mkdir -p "$SKILL_DST"
cp -f "$INFRA/skills/bash-mcp/SKILL.md" "$SKILL_DST/"
cp -f "$INFRA/skills/bash-mcp/_meta.json" "$SKILL_DST/"
echo "  -> copied to $SKILL_DST/"

# 4. mcp.json entry (idempotent overwrite of bash-mcp only)
echo ""
echo "[4/4] mcp.json entry"
WSL_IP=$(hostname -I | awk '{print $1}')
MCP_JSON="$WSL_USER_HOME/.minimax/mcp/mcp.json"
python3 - <<PYEOF
import json
from pathlib import Path
p = Path("$MCP_JSON")
if not p.exists():
    print(f"  ! $MCP_JSON does not exist — skipping (create it manually first)")
else:
    cfg = json.loads(p.read_text(encoding="utf-8"))
    desc = (
        "WSL bash executor \u2014 REQUIRED for any WSL command. "
        "Tools: run_command, check_env, list_binaries, which. "
        "DO NOT use `wsl -d ... -- bash -c \"...\"` from PowerShell \u2014 that path has "
        "quoting, UTF-16, and PATH bugs. Instead, call bash-mcp_run_command({command: \"...\"}). "
        "Safety: denylist + timeouts + output cap; pass dangerous=true to bypass soft denylist."
    )
    servers = cfg.setdefault("mcpServers", {})
    servers["bash-mcp"] = {
        "url": f"http://{WSL_IP}:54321/mcp/",
        "type": "streamable-http",
        "enabled": True,
        "configured": True,
        "description": desc,
    }
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  -> bash-mcp entry written (url: http://{WSL_IP}:54321/mcp/)")
PYEOF

# 5. Manual step for the mavis runtime registry (Windows-side)
echo ""
echo "============================================================"
echo "Manual step required from Windows PowerShell (as the user):"
echo ""
cat <<MCEOF
  mavis mcp create bash-mcp \`
    --transport streamable-http \`
    --url http://${WSL_IP}:54321/mcp/ \`
    --enabled true \`
    --description "WSL bash executor \u2014 REQUIRED for any WSL command. Tools: run_command, check_env, list_binaries, which. DO NOT use \`wsl -d ... -- bash -c \"...\"\` from PowerShell \u2014 quoting, UTF-16, and PATH bugs. Call bash-mcp_run_command({command: \"...\"}). Safety: denylist + timeouts + output cap; pass dangerous=true to bypass soft denylist."
MCEOF
echo ""
echo "(If bash-mcp is already registered, this is a no-op update.)"
echo "============================================================"
echo ""
echo "Verify:"
echo "  systemctl --user status bash-mcp.service"
echo "  mavis mcp list  (from PowerShell)"
echo "  curl http://${WSL_IP}:54321/mcp/  (initialize MCP, then tools/list)"
echo ""
echo "Done."
