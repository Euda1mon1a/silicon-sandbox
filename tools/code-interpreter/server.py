"""MCP Tool Server: Code Interpreter — execute code in Seatbelt sandboxes."""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)

SANDBOX_API = "http://127.0.0.1:8093"

# Track persistent sessions (session_id -> sandbox results history)
_sessions: dict[str, list[dict]] = {}

server = Server("code-interpreter")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="execute",
            description=(
                "Execute code in a sandboxed environment. "
                "Supports Python 3.12, Node.js, and Bash. "
                "Returns stdout, stderr, and exit code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The code to execute",
                    },
                    "language": {
                        "type": "string",
                        "description": "Programming language: python, node, bash",
                        "enum": ["python", "node", "bash"],
                        "default": "python",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Execution timeout in seconds (max 300)",
                        "default": 30,
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional session ID for persistent state across calls",
                    },
                    "tier": {
                        "type": "string",
                        "description": "Sandbox tier: A (Seatbelt/macOS), B (MicroVM/Linux), C (native)",
                        "enum": ["A", "B", "C"],
                        "default": "A",
                    },
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="upload_file",
            description="Upload a file into a sandbox execution environment.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the file to create",
                    },
                    "content": {
                        "type": "string",
                        "description": "File content",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session ID (file will be available in next execute call)",
                    },
                },
                "required": ["filename", "content"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "execute":
        return await _execute(arguments)
    elif name == "upload_file":
        return await _upload_file(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _execute(args: dict) -> list[TextContent]:
    """Execute code in a sandbox."""
    code = args.get("code", "")
    language = args.get("language", "python")
    timeout = min(args.get("timeout", 30), 300)
    tier = args.get("tier", "A")
    session_id = args.get("session_id", "")

    # Build command based on language
    if language == "python":
        command = f"python3 -c {_shell_quote(code)}"
    elif language == "node":
        command = f"node -e {_shell_quote(code)}"
    elif language == "bash":
        command = code
    else:
        return [TextContent(type="text", text=f"Unsupported language: {language}")]

    # Collect files from session if any
    files = {}
    if session_id and session_id in _sessions:
        for entry in _sessions[session_id]:
            if entry.get("type") == "file":
                files[entry["filename"]] = entry["content"]

    # Call sandbox engine
    try:
        async with httpx.AsyncClient(timeout=timeout + 30) as client:
            resp = await client.post(
                f"{SANDBOX_API}/sandbox",
                json={
                    "tier": tier,
                    "command": command,
                    "timeout": timeout,
                    "files": files,
                },
            )

        if resp.status_code != 200:
            return [TextContent(type="text", text=f"Sandbox API error: {resp.status_code}")]

        result = resp.json()

        # Track in session
        if session_id:
            if session_id not in _sessions:
                _sessions[session_id] = []
            _sessions[session_id].append({
                "type": "execution",
                "code": code,
                "result": result,
            })

        # Format output
        output_parts = []
        if result.get("stdout"):
            output_parts.append(f"STDOUT:\n{result['stdout']}")
        if result.get("stderr"):
            output_parts.append(f"STDERR:\n{result['stderr']}")
        output_parts.append(f"Exit code: {result.get('exit_code', 'unknown')}")
        output_parts.append(f"Status: {result.get('status', 'unknown')}")

        return [TextContent(type="text", text="\n".join(output_parts))]

    except Exception as e:
        return [TextContent(type="text", text=f"Execution error: {e}")]


async def _upload_file(args: dict) -> list[TextContent]:
    """Upload a file for future session use."""
    filename = args.get("filename", "")
    content = args.get("content", "")
    session_id = args.get("session_id", str(uuid.uuid4().hex[:8]))

    if not filename:
        return [TextContent(type="text", text="Filename is required")]

    if session_id not in _sessions:
        _sessions[session_id] = []

    _sessions[session_id].append({
        "type": "file",
        "filename": filename,
        "content": content,
    })

    return [TextContent(
        type="text",
        text=f"File '{filename}' uploaded to session {session_id} ({len(content)} bytes)",
    )]


def _shell_quote(s: str) -> str:
    """Shell-quote a string."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
