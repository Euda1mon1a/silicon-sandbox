#!/usr/bin/env bash
# SiliconSandbox — install script
# Sets up venv, dependencies, Swift build, VM images, and LaunchAgent plists.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"
PLIST_DIR="$HOME/Library/LaunchAgents"

echo "=== SiliconSandbox Installer ==="
echo "Root: $ROOT"

# 1. Python venv
echo ""
echo "[1/5] Python virtual environment..."
if [ ! -d "$VENV" ]; then
    python3.12 -m venv "$VENV"
    echo "  Created venv at $VENV"
else
    echo "  Venv already exists"
fi

echo "  Installing dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet \
    fastapi uvicorn httpx pydantic websockets \
    anthropic sqlite-vec \
    mcp duckduckgo-search readability-lxml lxml_html_clean \
    sse-starlette \
    pytest

echo "  Dependencies installed"

# 2. Swift vm-launcher build
echo ""
echo "[2/5] Swift vm-launcher..."
VM_LAUNCHER="$ROOT/vm-launcher"
if [ -d "$VM_LAUNCHER/Package.swift" ] || [ -f "$VM_LAUNCHER/Package.swift" ]; then
    cd "$VM_LAUNCHER"
    swift build -c release 2>/dev/null || echo "  Warning: Swift build failed (may need Virtualization.framework entitlements)"
    # Sign with entitlements
    if [ -f ".build/release/vm-launcher" ] && [ -f "vm-launcher.entitlements" ]; then
        codesign --entitlements vm-launcher.entitlements --force -s - .build/release/vm-launcher
        echo "  Built and signed vm-launcher"
    fi
    cd "$ROOT"
else
    echo "  Skipped (vm-launcher not found — MicroVM features unavailable)"
fi

# 3. VM images
echo ""
echo "[3/5] VM images..."
if [ -x "$ROOT/scripts/prepare-vm-images.sh" ]; then
    IMAGES_DIR="$ROOT/config/vm-images"
    if [ -f "$IMAGES_DIR/Image" ] && [ -f "$IMAGES_DIR/initramfs.cpio.gz" ]; then
        echo "  VM images already present"
    else
        echo "  Running prepare-vm-images.sh..."
        "$ROOT/scripts/prepare-vm-images.sh"
    fi
else
    echo "  Skipped (prepare-vm-images.sh not found)"
fi

# 4. Data directories
echo ""
echo "[4/5] Data directories..."
mkdir -p "$ROOT/data/workspaces/default"
mkdir -p "$ROOT/data"
echo "  Created data directories"

# 5. LaunchAgent plists
echo ""
echo "[5/5] LaunchAgent plists..."
mkdir -p "$PLIST_DIR"

for svc in engine orchestrator ui mcp; do
    PLIST="$PLIST_DIR/com.siliconsandbox.$svc.plist"
    case "$svc" in
        engine)
            PORT=8093
            MODULE="sandbox_engine.server"
            WORKDIR="$ROOT/sandbox-engine"
            ;;
        orchestrator)
            PORT=8094
            MODULE="orchestrator.server"
            WORKDIR="$ROOT/orchestrator"
            ;;
        ui)
            PORT=8095
            MODULE="ui.server"
            WORKDIR="$ROOT"
            ;;
        mcp)
            # MCP server uses direct script, not uvicorn module
            PLIST_SRC="$ROOT/launchd/com.siliconsandbox.mcp.plist"
            if [ -f "$PLIST_SRC" ]; then
                cp "$PLIST_SRC" "$PLIST"
                echo "  Created $PLIST"
            fi
            continue
            ;;
    esac

    cat > "$PLIST" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.siliconsandbox.$svc</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV/bin/python3</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>${MODULE}:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$WORKDIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$VENV/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>PYTHONPATH</key>
        <string>$ROOT</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>$ROOT/logs/$svc.out.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/logs/$svc.err.log</string>
</dict>
</plist>
PLISTEOF
    echo "  Created $PLIST"
done

mkdir -p "$ROOT/logs"

echo ""
echo "=== Installation complete ==="
echo "Run: scripts/launch.sh   to start all services"
echo "Run: scripts/stop.sh     to stop all services"
