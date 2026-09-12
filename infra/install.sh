#!/bin/bash
# ============================================================
# bash-mcp installer — idempotent.
# Run from the repo root: ./infra/install.sh [--dry-run]
#
# Deploys:
#   - systemd unit       → ~/.config/systemd/user/bash-mcp.service
#   - PreToolUse hook     → /mnt/c/Users/jesus/.minimax/agents/mavis/hooks/bash-mcp-redirect.md
#   - Skill               → /mnt/c/Users/jesus/.mavis/skills/bash-mcp/{SKILL.md,_meta.json}
#   - mcp.json entry      → /mnt/c/Users/jesus/.minimax/mcp/mcp.json (idempotent overwrite of bash-mcp)
#
# Flags:
#   --dry-run    Print what would happen without making any changes.
#
# The mavis mcp create command is always printed (manual step from PowerShell).
# ============================================================
set -e

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help)
            echo "Usage: $0 [--dry-run]"
            echo ""
            echo "Deploys bash-mcp to ~/.config/systemd/user/, /mnt/c/Users/jesus/.minimax/, /mnt/c/Users/jesus/.mavis/"
            echo "with --dry-run: print what would happen without making changes"
            exit 0
            ;;
        *) echo "Unknown argument: $arg" >&2; exit 2 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$REPO_ROOT/infra"
WSL_USER_HOME="/mnt/c/Users/jesus"

# Helper: run a command OR print it under --dry-run.
# Usage: run_or_print "step description" command [args...]
run_or_print() {
    local label="$1"; shift
    if [ "$DRY_RUN" = "1" ]; then
        printf '  [DRY-RUN] %s: ' "$label"
        printf '%q ' "$@"
        echo
    else
        "$@"
    fi
}

echo "=== bash-mcp install ==="
echo "Repo root: $REPO_ROOT"
echo "WSL home:  $WSL_USER_HOME"
[ "$DRY_RUN" = "1" ] && echo "MODE:      DRY-RUN (no changes will be made)"
echo ""

# 1. systemd unit
echo "[1/4] systemd unit"
run_or_print "mkdir"  mkdir -p ~/.config/systemd/user
run_or_print "cp"     cp -f "$INFRA/systemd/bash-mcp.service" ~/.config/systemd/user/
run_or_print "systemd" systemctl --user daemon-reload
run_or_print "systemd" systemctl --user enable bash-mcp.service
run_or_print "systemd" systemctl --user restart bash-mcp.service
[ "$DRY_RUN" = "1" ] && echo "  -> [DRY-RUN] would enable + restart service" \
                       || echo "  -> enabled + restarted"

# 2. PreToolUse hook
echo ""
echo "[2/4] PreToolUse hook"
HOOK_DST="$WSL_USER_HOME/.minimax/agents/mavis/hooks"
run_or_print "mkdir"  mkdir -p "$HOOK_DST"
run_or_print "cp"     cp -f "$INFRA/hooks/bash-mcp-redirect.md" "$HOOK_DST/"
run_or_print "cp"     cp -f "$INFRA/hooks/bash-mcp-redirect.js" "$HOOK_DST/"
[ "$DRY_RUN" = "1" ] && echo "  -> [DRY-RUN] would copy to $HOOK_DST/" \
                       || echo "  -> copied to $HOOK_DST/"

# 3. Skill
echo ""
echo "[3/4] Skill"
SKILL_DST="$WSL_USER_HOME/.mavis/skills/bash-mcp"
run_or_print "mkdir"  mkdir -p "$SKILL_DST"
run_or_print "cp"     cp -f "$INFRA/skills/bash-mcp/SKILL.md" "$SKILL_DST/"
run_or_print "cp"     cp -f "$INFRA/skills/bash-mcp/_meta.json" "$SKILL_DST/"
[ "$DRY_RUN" = "1" ] && echo "  -> [DRY-RUN] would copy to $SKILL_DST/" \
                       || echo "  -> copied to $SKILL_DST/"

# 4. mcp.json entry (idempotent overwrite of bash-mcp only)
echo ""
echo "[4/4] mcp.json entry"
WSL_IP=$(hostname -I | awk '{print $1}')
MCP_JSON="$WSL_USER_HOME/.minimax/mcp/mcp.json"
if [ "$DRY_RUN" = "1" ]; then
    echo "  [DRY-RUN] would edit: $MCP_JSON"
    echo "  [DRY-RUN] would set: bash-mcp.url = http://${WSL_IP}:54321/mcp/"
else
    # Pass values via env vars (heredoc-to-stdin does not expand bash variables
    # when piped to `python3 -`, so we cannot rely on heredoc substitution here).
    BASH_MCP_MCP_JSON="$MCP_JSON" BASH_MCP_WSL_IP="$WSL_IP" python3 -c '
import json, os
from pathlib import Path
mcp_path = Path(os.environ["BASH_MCP_MCP_JSON"])
wsl_ip = os.environ["BASH_MCP_WSL_IP"]
if not mcp_path.exists():
    print(f"  ! {mcp_path} does not exist — skipping (create it manually first)")
else:
    cfg = json.loads(mcp_path.read_text(encoding="utf-8"))
    desc = (
        "WSL bash executor \u2014 REQUIRED for any WSL command. "
        "Tools: run_command, check_env, list_binaries, which. "
        "DO NOT use `wsl -d ... -- bash -c \"...\"` from PowerShell \u2014 that path has "
        "quoting, UTF-16, and PATH bugs. Instead, call bash-mcp_run_command({command: \"...\"}). "
        "Safety: denylist + timeouts + output cap; pass dangerous=true to bypass soft denylist."
    )
    servers = cfg.setdefault("mcpServers", {})
    servers["bash-mcp"] = {
        "url": f"http://{wsl_ip}:54321/mcp/",
        "type": "streamable-http",
        "enabled": True,
        "configured": True,
        "description": desc,
    }
    mcp_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  -> bash-mcp entry written (url: http://{wsl_ip}:54321/mcp/)")
'
fi

# 5. Manual step for the mavis runtime registry (Windows-side; always printed)
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
echo "Verify after running for real:"
echo "  systemctl --user status bash-mcp.service"
echo "  mavis mcp list  (from PowerShell)"
echo "  curl http://${WSL_IP}:54321/mcp/  (initialize MCP, then tools/list)"
echo ""

if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN COMPLETE. Re-run without --dry-run to apply."
else
    echo "Done."
fi
