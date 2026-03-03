#!/usr/bin/env python3
"""
SiliconSandbox MCP Server — expose sandbox engine to Claude Code sessions.

Runs as a persistent HTTP server on 127.0.0.1:8096 (streamable-http transport).
Wraps the sandbox engine API on port 8093.

Tools:
  sandbox_run         — One-shot sandboxed command execution
  sandbox_health      — Engine health and tier availability
  session_create      — Create a persistent sandbox session
  session_exec        — Execute a command in an existing session
  session_write_files — Write files to a session workspace
  session_read_file   — Read a file from a session workspace
  session_list        — List all active sessions
  session_destroy     — Destroy a session and clean up
  session_pause       — Pause a session (SIGSTOP)
  session_resume      — Resume a paused session (SIGCONT)
"""

import json
import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP

server = FastMCP("silicon-sandbox", host="127.0.0.1", port=8096)

ENGINE_URL = os.environ.get("SILICONSANDBOX_ENGINE_URL", "http://127.0.0.1:8093")
_auth_token = os.environ.get("SILICONSANDBOX_AUTH_TOKEN")


def _headers() -> dict[str, str]:
    """Build request headers including auth if configured."""
    if _auth_token:
        return {"Authorization": f"Bearer {_auth_token}"}
    return {}


def _engine_client(timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(base_url=ENGINE_URL, timeout=timeout, headers=_headers())


# ── One-shot sandbox ──


@server.tool()
def sandbox_run(
    command: str,
    tier: str = "A",
    timeout: int = 30,
    language: str = "bash",
    allow_network: bool = False,
    files: dict[str, str] | None = None,
) -> str:
    """Execute a command in an isolated sandbox and return the result.

    Args:
        command: The command or code to execute.
        tier: Sandbox tier — "A" (Seatbelt/macOS, default), "B" (MicroVM/Linux), "C" (native).
        timeout: Max execution time in seconds (1-600).
        language: If "python" or "node", wraps code appropriately. "bash" runs as-is.
        allow_network: Whether to allow outbound network access (default: false).
        files: Optional dict of filename→content to inject into the workspace.

    Returns:
        Formatted result with stdout, stderr, exit code, and status.
    """
    # Wrap code for non-bash languages
    if language == "python":
        command = f"python3 -c {_shell_quote(command)}"
    elif language == "node":
        command = f"node -e {_shell_quote(command)}"

    payload = {
        "tier": tier,
        "command": command,
        "timeout": min(timeout, 600),
        "allow_network": allow_network,
    }
    if files:
        payload["files"] = files

    try:
        with _engine_client(timeout=timeout + 30) as client:
            resp = client.post("/sandbox", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable — is the sandbox engine running on port 8093?"

    parts = []
    if data.get("stdout"):
        parts.append(f"STDOUT:\n{data['stdout']}")
    if data.get("stderr"):
        parts.append(f"STDERR:\n{data['stderr']}")
    parts.append(f"Exit code: {data.get('exit_code', 'unknown')}")
    parts.append(f"Status: {data.get('status', 'unknown')}")
    parts.append(f"Tier: {data.get('tier', 'unknown')}")
    parts.append(f"Elapsed: {data.get('elapsed_seconds', 0):.2f}s")
    if data.get("violations"):
        parts.append(f"Violations: {', '.join(data['violations'])}")
    return "\n".join(parts)


# ── Health ──


@server.tool()
def sandbox_health() -> str:
    """Check sandbox engine health and available tiers.

    Returns:
        JSON health response including tier availability, active sandbox count, and version.
    """
    try:
        with _engine_client(timeout=5.0) as client:
            resp = client.get("/health")
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)
    except httpx.ConnectError:
        return "Engine unavailable — is the sandbox engine running on port 8093?"


# ── Persistent sessions ──


@server.tool()
def session_create(
    tier: str = "A",
    allow_network: bool = False,
    ttl_seconds: int = 3600,
    files: dict[str, str] | None = None,
) -> str:
    """Create a persistent sandbox session with a long-lived workspace.

    Sessions persist across multiple exec calls. Use for multi-step workflows
    where you need to build up state (install packages, create files, etc.).

    Args:
        tier: "A" (Seatbelt) or "C" (native). Default: "A".
        allow_network: Enable outbound network. Default: false.
        ttl_seconds: Session TTL (60-86400). Default: 3600 (1 hour).
        files: Optional initial files to inject into workspace.

    Returns:
        Session ID and info. Use the session_id with session_exec, session_write_files, etc.
    """
    payload = {
        "tier": tier,
        "allow_network": allow_network,
        "ttl_seconds": ttl_seconds,
    }
    if files:
        payload["files"] = files

    try:
        with _engine_client() as client:
            resp = client.post("/session", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable — is the sandbox engine running on port 8093?"

    return (
        f"Session created: {data['id']}\n"
        f"Tier: {data['tier']}\n"
        f"Status: {data['status']}\n"
        f"TTL: {data['ttl_seconds']}s\n"
        f"Workspace: {data.get('workspace', 'N/A')}"
    )


@server.tool()
def session_exec(
    session_id: str,
    command: str,
    timeout: int = 30,
    language: str = "bash",
) -> str:
    """Execute a command in an existing persistent session.

    Args:
        session_id: The session ID (from session_create).
        command: The command or code to execute.
        timeout: Max execution time in seconds (1-600).
        language: "bash" (default), "python", or "node".

    Returns:
        Execution result with stdout, stderr, exit code.
    """
    if language == "python":
        command = f"python3 -c {_shell_quote(command)}"
    elif language == "node":
        command = f"node -e {_shell_quote(command)}"

    try:
        with _engine_client(timeout=timeout + 30) as client:
            resp = client.post(
                f"/session/{session_id}/exec",
                json={"command": command, "timeout": min(timeout, 600)},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable — is the sandbox engine running on port 8093?"

    parts = []
    if data.get("stdout"):
        parts.append(f"STDOUT:\n{data['stdout']}")
    if data.get("stderr"):
        parts.append(f"STDERR:\n{data['stderr']}")
    parts.append(f"Exit code: {data.get('exit_code', 'unknown')}")
    parts.append(f"Elapsed: {data.get('elapsed_seconds', 0):.2f}s")
    if data.get("violations"):
        parts.append(f"Violations: {', '.join(data['violations'])}")
    return "\n".join(parts)


@server.tool()
def session_write_files(session_id: str, files: dict[str, str]) -> str:
    """Write files into a session workspace.

    Args:
        session_id: The session ID.
        files: Dict of filename→content pairs to write.

    Returns:
        List of written filenames.
    """
    try:
        with _engine_client() as client:
            resp = client.post(f"/session/{session_id}/files", json={"files": files})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"

    return f"Written: {', '.join(data.get('written', []))}"


@server.tool()
def session_read_file(session_id: str, path: str) -> str:
    """Read a file from a session workspace.

    Args:
        session_id: The session ID.
        path: Relative path within the workspace (no absolute paths or ..).

    Returns:
        File contents as text.
    """
    try:
        with _engine_client() as client:
            resp = client.get(f"/session/{session_id}/files/{path}")
            resp.raise_for_status()
            return resp.text
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"


@server.tool()
def session_list() -> str:
    """List all sandbox sessions (active, paused, and destroyed).

    Returns:
        Formatted list of sessions with IDs, tiers, status, and exec counts.
    """
    try:
        with _engine_client() as client:
            resp = client.get("/sessions")
            resp.raise_for_status()
            sessions = resp.json()
    except httpx.ConnectError:
        return "Engine unavailable"

    if not sessions:
        return "No sessions."

    lines = []
    for s in sessions:
        status = s.get("status", "unknown")
        paused = " (PAUSED)" if s.get("paused") else ""
        lines.append(
            f"  {s['id']}  tier={s['tier']}  status={status}{paused}  "
            f"exec_count={s.get('exec_count', 0)}  ttl={s.get('ttl_seconds', 0)}s"
        )
    return f"Sessions ({len(sessions)}):\n" + "\n".join(lines)


@server.tool()
def session_destroy(session_id: str) -> str:
    """Destroy a session and clean up its workspace.

    Args:
        session_id: The session ID to destroy.

    Returns:
        Confirmation of destruction.
    """
    try:
        with _engine_client() as client:
            resp = client.delete(f"/session/{session_id}")
            resp.raise_for_status()
            return f"Session {session_id} destroyed."
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"


@server.tool()
def session_pause(session_id: str) -> str:
    """Pause a session — suspend all running processes (SIGSTOP).

    Paused sessions don't count toward idle timeout but still count toward TTL.
    Resume with session_resume.

    Args:
        session_id: The session ID to pause.

    Returns:
        Pause confirmation.
    """
    try:
        with _engine_client() as client:
            resp = client.post(f"/session/{session_id}/pause")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"

    return f"Session {session_id}: {data.get('status', 'unknown')} ({data.get('stopped_pids', 0)} processes stopped)"


@server.tool()
def session_resume(session_id: str) -> str:
    """Resume a paused session — continue all suspended processes (SIGCONT).

    Args:
        session_id: The session ID to resume.

    Returns:
        Resume confirmation.
    """
    try:
        with _engine_client() as client:
            resp = client.post(f"/session/{session_id}/resume")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"

    return f"Session {session_id}: {data.get('status', 'unknown')} ({data.get('resumed_pids', 0)} processes resumed)"


# ── Helpers ──


def _shell_quote(s: str) -> str:
    """Shell-quote a string for safe embedding in bash commands."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    server.run(transport="streamable-http")
