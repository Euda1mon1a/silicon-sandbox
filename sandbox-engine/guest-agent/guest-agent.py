#!/usr/bin/env python3
"""SiliconSandbox Guest Agent — runs inside the MicroVM.

Listens on vsock for JSON-RPC commands from the host.
Handles: exec, write_file, read_file, ping, shutdown.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

VSOCK_CID_HOST = 2  # CID for host
VSOCK_PORT = 1024


def log(msg):
    """Write to stderr (goes to serial console)."""
    sys.stderr.write(f"[guest-agent] {msg}\n")
    sys.stderr.flush()


def handle_ping(params):
    return {"status": "ok", "hostname": socket.gethostname(), "pid": os.getpid()}


def handle_exec(params):
    """Execute a command and return stdout/stderr/exit_code."""
    command = params.get("command", "")
    timeout = params.get("timeout", 30)
    cwd = params.get("cwd", "/workspace")
    env = params.get("env", {})

    exec_env = os.environ.copy()
    exec_env.update(env)
    exec_env["HOME"] = "/root"
    exec_env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

    os.makedirs(cwd, exist_ok=True)

    try:
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=exec_env,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "status": "completed",
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "exit_code": -1,
            "status": "timeout",
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
            "status": "error",
        }


def handle_write_file(params):
    """Write content to a file in the workspace."""
    path = params.get("path", "")
    content = params.get("content", "")
    if not path:
        return {"error": "path is required"}

    # Resolve relative to /workspace
    if not os.path.isabs(path):
        path = os.path.join("/workspace", path)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return {"path": path, "size": len(content)}


def handle_read_file(params):
    """Read a file from the workspace."""
    path = params.get("path", "")
    if not path:
        return {"error": "path is required"}

    if not os.path.isabs(path):
        path = os.path.join("/workspace", path)

    try:
        with open(path, "r") as f:
            content = f.read()
        return {"path": path, "content": content, "size": len(content)}
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except Exception as e:
        return {"error": str(e)}


def handle_shutdown(params):
    """Shut down the VM."""
    log("Shutdown requested")
    # Write response before shutting down
    os.system("poweroff -f")
    return {"status": "shutting_down"}


HANDLERS = {
    "ping": handle_ping,
    "exec": handle_exec,
    "write_file": handle_write_file,
    "read_file": handle_read_file,
    "shutdown": handle_shutdown,
}


def process_request(data):
    """Process a JSON-RPC request and return a response."""
    try:
        request = json.loads(data)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}", "id": None})

    method = request.get("method", "")
    params = request.get("params", {})
    req_id = request.get("id", None)

    handler = HANDLERS.get(method)
    if not handler:
        return json.dumps({"error": f"Unknown method: {method}", "id": req_id})

    try:
        result = handler(params)
        return json.dumps({"result": result, "id": req_id})
    except Exception as e:
        return json.dumps({"error": str(e), "id": req_id})


def run_vsock_server():
    """Listen on vsock for JSON-RPC commands."""
    log(f"Starting vsock server on port {VSOCK_PORT}")

    # AF_VSOCK = 40 on Linux
    AF_VSOCK = 40
    sock = socket.socket(AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # CID_ANY = -1 (0xFFFFFFFF), listen on any CID
    CID_ANY = 0xFFFFFFFF
    sock.bind((CID_ANY, VSOCK_PORT))
    sock.listen(5)
    log(f"Listening on vsock port {VSOCK_PORT}")

    # Signal readiness on serial console
    print("AGENT_READY", flush=True)

    while True:
        try:
            conn, addr = sock.accept()
            log(f"Connection from CID {addr[0]}")

            # Read the full request (newline-delimited)
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break

            if data:
                request_str = data.decode("utf-8").strip()
                log(f"Request: {request_str[:200]}")
                response = process_request(request_str)
                conn.sendall((response + "\n").encode("utf-8"))
                log(f"Response sent ({len(response)} bytes)")

            conn.close()
        except Exception as e:
            log(f"Error: {e}")
            try:
                conn.close()
            except:
                pass


if __name__ == "__main__":
    log("Guest agent starting")
    run_vsock_server()
