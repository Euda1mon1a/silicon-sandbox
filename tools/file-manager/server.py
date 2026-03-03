"""MCP Tool Server: File Manager — scoped workspace file operations."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)

# Workspace root — all operations scoped here
WORKSPACE_ROOT = Path.home() / "workspace" / "silicon-sandbox" / "data" / "workspaces"

server = Server("file-manager")


def _resolve_path(path: str, workspace: str = "default") -> Path:
    """Resolve a path within the workspace, preventing traversal."""
    ws_root = WORKSPACE_ROOT / workspace
    ws_root.mkdir(parents=True, exist_ok=True)

    resolved = (ws_root / path).resolve()
    # Prevent path traversal
    if not str(resolved).startswith(str(ws_root.resolve())):
        raise ValueError(f"Path traversal detected: {path}")
    return resolved


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="read",
            description="Read a file from the workspace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace"},
                    "workspace": {"type": "string", "description": "Workspace name", "default": "default"},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="write",
            description="Write content to a file in the workspace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace"},
                    "content": {"type": "string", "description": "File content to write"},
                    "workspace": {"type": "string", "description": "Workspace name", "default": "default"},
                },
                "required": ["path", "content"],
            },
        ),
        Tool(
            name="list",
            description="List files in a workspace directory.",
            inputSchema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Directory path (default: root)", "default": "."},
                    "workspace": {"type": "string", "description": "Workspace name", "default": "default"},
                },
            },
        ),
        Tool(
            name="search",
            description="Search for files matching a pattern.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g., '*.py', '**/*.txt')"},
                    "workspace": {"type": "string", "description": "Workspace name", "default": "default"},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="delete",
            description="Delete a file from the workspace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to delete"},
                    "workspace": {"type": "string", "description": "Workspace name", "default": "default"},
                },
                "required": ["path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "read":
            return await _read(arguments)
        elif name == "write":
            return await _write(arguments)
        elif name == "list":
            return await _list(arguments)
        elif name == "search":
            return await _search(arguments)
        elif name == "delete":
            return await _delete(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except ValueError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _read(args: dict) -> list[TextContent]:
    path = _resolve_path(args["path"], args.get("workspace", "default"))
    if not path.exists():
        return [TextContent(type="text", text=f"File not found: {args['path']}")]
    if not path.is_file():
        return [TextContent(type="text", text=f"Not a file: {args['path']}")]
    content = path.read_text(errors="replace")
    return [TextContent(type="text", text=content)]


async def _write(args: dict) -> list[TextContent]:
    path = _resolve_path(args["path"], args.get("workspace", "default"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"])
    return [TextContent(type="text", text=f"Written {len(args['content'])} bytes to {args['path']}")]


async def _list(args: dict) -> list[TextContent]:
    directory = args.get("directory", ".")
    path = _resolve_path(directory, args.get("workspace", "default"))
    if not path.exists():
        return [TextContent(type="text", text=f"Directory not found: {directory}")]
    if not path.is_dir():
        return [TextContent(type="text", text=f"Not a directory: {directory}")]

    entries = []
    for item in sorted(path.iterdir()):
        rel = item.relative_to((WORKSPACE_ROOT / args.get("workspace", "default")).resolve())
        suffix = "/" if item.is_dir() else ""
        size = item.stat().st_size if item.is_file() else 0
        entries.append(f"  {rel}{suffix}  ({size} bytes)" if not suffix else f"  {rel}{suffix}")

    if not entries:
        return [TextContent(type="text", text="(empty directory)")]

    return [TextContent(type="text", text="\n".join(entries))]


async def _search(args: dict) -> list[TextContent]:
    pattern = args["pattern"]
    workspace = args.get("workspace", "default")
    ws_root = WORKSPACE_ROOT / workspace

    if not ws_root.exists():
        return [TextContent(type="text", text="Workspace not found")]

    matches = list(ws_root.glob(pattern))
    if not matches:
        return [TextContent(type="text", text=f"No files matching '{pattern}'")]

    results = []
    for m in matches[:100]:  # Cap at 100 results
        rel = m.relative_to(ws_root)
        results.append(str(rel))

    return [TextContent(type="text", text="\n".join(results))]


async def _delete(args: dict) -> list[TextContent]:
    path = _resolve_path(args["path"], args.get("workspace", "default"))
    if not path.exists():
        return [TextContent(type="text", text=f"File not found: {args['path']}")]
    if path.is_dir():
        return [TextContent(type="text", text="Cannot delete directories")]
    path.unlink()
    return [TextContent(type="text", text=f"Deleted {args['path']}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
