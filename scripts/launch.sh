#!/usr/bin/env bash
# SiliconSandbox — start all services via LaunchAgents
set -euo pipefail

UID_NUM=$(id -u)
SERVICES=(engine orchestrator ui mcp)

echo "Starting SiliconSandbox services..."

for svc in "${SERVICES[@]}"; do
    PLIST="$HOME/Library/LaunchAgents/com.siliconsandbox.$svc.plist"
    if [ ! -f "$PLIST" ]; then
        echo "  Warning: $PLIST not found — run install.sh first"
        continue
    fi

    # Unload if already loaded (ignore errors)
    launchctl bootout "gui/$UID_NUM" "$PLIST" 2>/dev/null || true

    # Load
    launchctl bootstrap "gui/$UID_NUM" "$PLIST"
    echo "  Started com.siliconsandbox.$svc"
done

echo ""
echo "Services:"
echo "  Engine:       http://127.0.0.1:8093"
echo "  Orchestrator: http://127.0.0.1:8094"
echo "  Web UI:       http://127.0.0.1:8095"
echo "  MCP Server:   http://127.0.0.1:8096/mcp"
echo ""
echo "Check health: curl -s http://127.0.0.1:8095/health | python3 -m json.tool"
