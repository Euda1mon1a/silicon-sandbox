"""MCP Tool Server: Web Research — search and fetch web content."""

from __future__ import annotations

import logging

import httpx
from duckduckgo_search import DDGS
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from readability import Document

logger = logging.getLogger(__name__)

server = Server("web-research")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search",
            description="Search the web using DuckDuckGo. Returns structured results with titles, URLs, and snippets.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results (max 20)",
                        "default": 5,
                    },
                    "region": {
                        "type": "string",
                        "description": "Region code (e.g., 'us-en')",
                        "default": "us-en",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="fetch_page",
            description="Fetch a web page and extract its main content as clean text.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_length": {
                        "type": "integer",
                        "description": "Maximum content length in characters",
                        "default": 10000,
                    },
                },
                "required": ["url"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "search":
            return await _search(arguments)
        elif name == "fetch_page":
            return await _fetch_page(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _search(args: dict) -> list[TextContent]:
    """Search DuckDuckGo."""
    query = args["query"]
    num_results = min(args.get("num_results", 5), 20)

    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=num_results))

    if not results:
        return [TextContent(type="text", text=f"No results for '{query}'")]

    formatted = []
    for i, r in enumerate(results, 1):
        formatted.append(
            f"{i}. **{r.get('title', 'No title')}**\n"
            f"   URL: {r.get('href', 'N/A')}\n"
            f"   {r.get('body', 'No snippet')}"
        )

    return [TextContent(type="text", text="\n\n".join(formatted))]


async def _fetch_page(args: dict) -> list[TextContent]:
    """Fetch and extract readable content from a URL."""
    url = args["url"]
    max_length = args.get("max_length", 10000)

    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "SiliconSandbox/0.2.0 (research-agent)"},
    ) as client:
        resp = await client.get(url)

    if resp.status_code != 200:
        return [TextContent(type="text", text=f"HTTP {resp.status_code} for {url}")]

    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type and "text/plain" not in content_type:
        return [TextContent(type="text", text=f"Unsupported content type: {content_type}")]

    html = resp.text

    # Extract readable content
    doc = Document(html)
    title = doc.title()
    content = doc.summary()

    # Strip remaining HTML tags (basic approach)
    import re
    text = re.sub(r'<[^>]+>', '', content)
    text = re.sub(r'\s+', ' ', text).strip()

    if len(text) > max_length:
        text = text[:max_length] + "... (truncated)"

    return [TextContent(type="text", text=f"# {title}\n\n{text}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
