#!/bin/bash
# sandbox-run.sh — Execute a command in SiliconSandbox Tier A
# Usage: sandbox-run.sh <command> [timeout] [tier]
# Falls back to direct execution if sandbox engine is unreachable.
set -uo pipefail

COMMAND="$1"
TIMEOUT="${2:-60}"
TIER="${3:-A}"
ENGINE_URL="http://127.0.0.1:8093"

# Health check — if engine is down, fall back to direct execution
if ! curl -sf --max-time 2 "$ENGINE_URL/health" >/dev/null 2>&1; then
    eval "$COMMAND"
    exit $?
fi

# Escape the command for JSON
COMMAND_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$COMMAND")

# POST to sandbox engine
RESPONSE=$(curl -sf --max-time "$((TIMEOUT + 10))" \
    -X POST "$ENGINE_URL/sandbox" \
    -H 'Content-Type: application/json' \
    -d "{\"command\":${COMMAND_JSON},\"tier\":\"${TIER}\",\"timeout\":${TIMEOUT}}" \
    2>/dev/null)

if [ $? -ne 0 ] || [ -z "$RESPONSE" ]; then
    eval "$COMMAND"
    exit $?
fi

# Extract results
EXIT_CODE=$(echo "$RESPONSE" | python3 -c "import json,sys; r=json.load(sys.stdin); print(r.get('exit_code', 1))" 2>/dev/null)
STDOUT=$(echo "$RESPONSE" | python3 -c "import json,sys; r=json.load(sys.stdin); print(r.get('stdout',''),end='')" 2>/dev/null)
STDERR=$(echo "$RESPONSE" | python3 -c "import json,sys; r=json.load(sys.stdin); print(r.get('stderr',''),end='')" 2>/dev/null)

[ -n "$STDOUT" ] && printf '%s\n' "$STDOUT"
[ -n "$STDERR" ] && printf '%s\n' "$STDERR" >&2

exit "${EXIT_CODE:-1}"
