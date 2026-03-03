"""MCP Tool Server: Browser Automation — Playwright in Tier B MicroVM.

NOTE: Full Playwright support requires the 'playwright' VM checkpoint (Phase 3).
Until that checkpoint exists, this server provides a fetch-based fallback.
"""

from __future__ import annotations

import logging

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)

SANDBOX_API = "http://127.0.0.1:8093"

server = Server("browser-automation")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="navigate",
            description="Navigate to a URL and return the page content. Uses a MicroVM sandbox for isolation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                    "wait_seconds": {
                        "type": "integer",
                        "description": "Seconds to wait for page load",
                        "default": 5,
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="screenshot",
            description="Take a screenshot of a URL (requires Playwright checkpoint).",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to screenshot"},
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="extract_text",
            description="Extract text content from a URL, optionally filtered by CSS selector.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to extract from"},
                    "selector": {
                        "type": "string",
                        "description": "CSS selector to filter content (optional)",
                    },
                },
                "required": ["url"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "navigate":
            return await _navigate(arguments)
        elif name == "screenshot":
            return await _screenshot(arguments)
        elif name == "extract_text":
            return await _extract_text(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _navigate(args: dict) -> list[TextContent]:
    """Navigate to URL using wget in a MicroVM (fallback until Playwright checkpoint)."""
    url = args["url"]

    # Use Tier B MicroVM with networking for isolated browsing
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{SANDBOX_API}/sandbox",
                json={
                    "tier": "B",
                    "command": f"wget -qO- --timeout=10 '{url}' 2>&1 | head -c 20000",
                    "timeout": 30,
                    "memory_gb": 1,
                    "cpus": 1,
                    "allow_network": True,
                    "allowed_domains": [_extract_domain(url)],
                },
            )

        if resp.status_code != 200:
            return [TextContent(type="text", text=f"Sandbox error: {resp.status_code}")]

        result = resp.json()
        content = result.get("stdout", "")

        # Basic HTML cleanup
        if "<html" in content.lower():
            import re
            # Strip scripts and styles
            content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL | re.IGNORECASE)
            content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL | re.IGNORECASE)
            content = re.sub(r'<[^>]+>', ' ', content)
            content = re.sub(r'\s+', ' ', content).strip()

        if not content:
            return [TextContent(type="text", text=f"No content retrieved from {url}")]

        return [TextContent(type="text", text=f"Content from {url}:\n\n{content[:10000]}")]

    except Exception as e:
        return [TextContent(type="text", text=f"Navigation error: {e}")]


async def _screenshot(args: dict) -> list[TextContent]:
    """Take a screenshot (stub — requires Playwright checkpoint)."""
    return [TextContent(
        type="text",
        text="Screenshot requires the 'playwright' VM checkpoint which is not yet built. "
        "Use 'navigate' or 'extract_text' for now.",
    )]


async def _extract_text(args: dict) -> list[TextContent]:
    """Extract text from URL using readability."""
    url = args["url"]

    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "SiliconSandbox/0.2.0"},
    ) as client:
        resp = await client.get(url)

    if resp.status_code != 200:
        return [TextContent(type="text", text=f"HTTP {resp.status_code}")]

    from readability import Document
    import re

    doc = Document(resp.text)
    title = doc.title()
    content = doc.summary()
    text = re.sub(r'<[^>]+>', '', content)
    text = re.sub(r'\s+', ' ', text).strip()

    return [TextContent(type="text", text=f"# {title}\n\n{text[:10000]}")]


def _extract_domain(url: str) -> str:
    """Extract domain from URL for allowlist."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return parsed.hostname or ""


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
