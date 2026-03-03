#!/bin/sh
# SiliconSandbox Guest Agent — shell version for Alpine minirootfs
# Listens on vsock via socat and handles JSON-RPC commands.
# Uses busybox tools only (no Python dependency).

VSOCK_PORT=1024
WORKSPACE="/workspace"

log() {
    echo "[guest-agent] $1" >&2
}

# Process a single JSON-RPC request from stdin, write response to stdout
process_request() {
    local input="$1"

    # Extract method using sed (busybox compatible)
    local method=$(echo "$input" | sed -n 's/.*"method"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
    local req_id=$(echo "$input" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p')

    case "$method" in
        ping)
            echo "{\"result\":{\"status\":\"ok\",\"hostname\":\"$(hostname)\"},\"id\":$req_id}"
            ;;
        exec)
            local command=$(echo "$input" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
            local timeout=$(echo "$input" | sed -n 's/.*"timeout"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p')
            timeout=${timeout:-30}

            # Execute command, capture output
            local tmpout=$(mktemp)
            local tmperr=$(mktemp)
            cd "$WORKSPACE"
            /bin/sh -c "$command" > "$tmpout" 2> "$tmperr" &
            local cmd_pid=$!

            # Wait with timeout
            local elapsed=0
            while kill -0 $cmd_pid 2>/dev/null; do
                sleep 1
                elapsed=$((elapsed + 1))
                if [ "$elapsed" -ge "$timeout" ]; then
                    kill -9 $cmd_pid 2>/dev/null
                    wait $cmd_pid 2>/dev/null
                    echo "{\"result\":{\"stdout\":\"\",\"stderr\":\"timeout\",\"exit_code\":-1,\"status\":\"timeout\"},\"id\":$req_id}"
                    rm -f "$tmpout" "$tmperr"
                    return
                fi
            done

            wait $cmd_pid
            local exit_code=$?
            local stdout=$(cat "$tmpout" | sed 's/"/\\"/g' | tr '\n' ' ')
            local stderr=$(cat "$tmperr" | sed 's/"/\\"/g' | tr '\n' ' ')
            rm -f "$tmpout" "$tmperr"

            echo "{\"result\":{\"stdout\":\"$stdout\",\"stderr\":\"$stderr\",\"exit_code\":$exit_code,\"status\":\"completed\"},\"id\":$req_id}"
            ;;
        write_file)
            local path=$(echo "$input" | sed -n 's/.*"path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
            local content=$(echo "$input" | sed -n 's/.*"content"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
            case "$path" in
                /*) ;;
                *) path="$WORKSPACE/$path" ;;
            esac
            mkdir -p "$(dirname "$path")"
            echo "$content" > "$path"
            echo "{\"result\":{\"path\":\"$path\"},\"id\":$req_id}"
            ;;
        read_file)
            local path=$(echo "$input" | sed -n 's/.*"path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
            case "$path" in
                /*) ;;
                *) path="$WORKSPACE/$path" ;;
            esac
            if [ -f "$path" ]; then
                local content=$(cat "$path" | sed 's/"/\\"/g' | tr '\n' ' ')
                echo "{\"result\":{\"path\":\"$path\",\"content\":\"$content\"},\"id\":$req_id}"
            else
                echo "{\"error\":\"File not found: $path\",\"id\":$req_id}"
            fi
            ;;
        shutdown)
            echo "{\"result\":{\"status\":\"shutting_down\"},\"id\":$req_id}"
            poweroff -f &
            ;;
        *)
            echo "{\"error\":\"Unknown method: $method\",\"id\":$req_id}"
            ;;
    esac
}

log "Starting shell-based guest agent on vsock port $VSOCK_PORT"
echo "AGENT_READY"

# Main loop: accept connections on vsock
# Since Alpine minirootfs has no socat, we use a simple approach:
# Read from /dev/hvc0 (serial console) as the communication channel
while true; do
    read -r line < /dev/hvc0
    if [ -n "$line" ]; then
        response=$(process_request "$line")
        echo "$response" > /dev/hvc0
    fi
done
