#!/usr/bin/env bash
# SiliconSandbox — stop all services
set -euo pipefail

UID_NUM=$(id -u)
SERVICES=(mcp ui orchestrator engine)  # Reverse order: MCP + UI first, engine last

echo "Stopping SiliconSandbox services..."

for svc in "${SERVICES[@]}"; do
    PLIST="$HOME/Library/LaunchAgents/com.siliconsandbox.$svc.plist"
    if [ ! -f "$PLIST" ]; then
        continue
    fi

    launchctl bootout "gui/$UID_NUM" "$PLIST" 2>/dev/null && \
        echo "  Stopped com.siliconsandbox.$svc" || \
        echo "  com.siliconsandbox.$svc was not running"
done

echo "All services stopped."
