"""MCP Tool Server: Browser Automation — Desktop VM sandbox with real Chromium.

Uses Tier B desktop MicroVM sessions with Xvfb + Chromium + CDP for full
browser automation in isolation. Supports screenshots, input injection,
navigation, and DOM operations via Chrome DevTools Protocol.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

server = FastMCP("browser-automation", host="127.0.0.1", port=8101)

ENGINE_URL = os.environ.get("SILICONSANDBOX_ENGINE_URL", "http://127.0.0.1:8093")
_auth_token = os.environ.get("SILICONSANDBOX_AUTH_TOKEN")


def _headers() -> dict[str, str]:
    if _auth_token:
        return {"Authorization": f"Bearer {_auth_token}"}
    return {}


def _client(timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(base_url=ENGINE_URL, timeout=timeout, headers=_headers())


# ── Desktop Session Management ──


@server.tool()
def desktop_session_create(
    memory_gb: int = 4,
    cpus: int = 4,
    allow_network: bool = True,
    ttl_seconds: int = 3600,
) -> str:
    """Create a persistent desktop VM session with Xvfb + Chromium.

    The session runs an isolated Alpine Linux VM with a virtual display (1280x720),
    Openbox window manager, and Chromium browser. Use the returned session_id with
    other desktop tools.

    Args:
        memory_gb: VM memory in GB (1-8, default 4).
        cpus: VM CPU count (1-8, default 4).
        allow_network: Allow outbound network access (default true for browsing).
        ttl_seconds: Session lifetime in seconds (60-86400, default 3600).

    Returns:
        Session ID and boot status.
    """
    try:
        with _client(timeout=45) as c:
            resp = c.post("/session", json={
                "tier": "B",
                "image": "desktop",
                "memory_gb": memory_gb,
                "cpus": cpus,
                "allow_network": allow_network,
                "ttl_seconds": ttl_seconds,
            })
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable — is the sandbox engine running on port 8093?"

    return (
        f"Desktop session created: {data['id']}\n"
        f"Tier: {data['tier']}\n"
        f"Status: {data['status']}\n"
        f"TTL: {data['ttl_seconds']}s\n"
        f"Display: Xvfb :99 (1280x720x24)\n"
        f"Browser: Chromium with CDP on localhost:9222"
    )


@server.tool()
def desktop_session_destroy(session_id: str) -> str:
    """Destroy a desktop VM session and free resources.

    Args:
        session_id: The desktop session ID.
    """
    try:
        with _client() as c:
            resp = c.delete(f"/session/{session_id}")
            resp.raise_for_status()
            return f"Desktop session {session_id} destroyed."
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"


# ── Screenshot ──


@server.tool()
def desktop_screenshot(
    session_id: str,
    format: str = "png",
    region: str | None = None,
) -> str:
    """Capture a screenshot of the desktop VM's virtual display.

    Args:
        session_id: The desktop session ID.
        format: Image format ("png" or "jpeg").
        region: Optional region "x,y,width,height" to capture a sub-area.

    Returns:
        Base64-encoded image data with format and size info.
    """
    payload = {"format": format}
    if region:
        payload["region"] = region

    try:
        with _client(timeout=15) as c:
            resp = c.post(f"/session/{session_id}/screenshot", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"

    image_b64 = data.get("image_b64", "")
    size = data.get("size", "unknown")
    return (
        f"Screenshot captured ({format}, {size})\n"
        f"Base64 length: {len(image_b64)} chars\n"
        f"Data: {image_b64[:200]}..."
    )


# ── Input Injection ──


@server.tool()
def desktop_click(
    session_id: str,
    x: int,
    y: int,
    button: int = 1,
) -> str:
    """Click at coordinates on the desktop VM's virtual display.

    Args:
        session_id: The desktop session ID.
        x: X coordinate (0 = left edge).
        y: Y coordinate (0 = top edge).
        button: Mouse button (1=left, 2=middle, 3=right).
    """
    try:
        with _client() as c:
            resp = c.post(f"/session/{session_id}/input", json={
                "action": "click", "x": x, "y": y, "button": button,
            })
            resp.raise_for_status()
            return f"Clicked at ({x}, {y}) button={button}"
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"


@server.tool()
def desktop_type(session_id: str, text: str) -> str:
    """Type text on the desktop VM's virtual display.

    Args:
        session_id: The desktop session ID.
        text: Text to type (sent as keystrokes).
    """
    try:
        with _client() as c:
            resp = c.post(f"/session/{session_id}/input", json={
                "action": "type", "text": text,
            })
            resp.raise_for_status()
            return f"Typed: {text[:50]}{'...' if len(text) > 50 else ''}"
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"


@server.tool()
def desktop_key(session_id: str, combo: str) -> str:
    """Press a key or key combination on the desktop VM.

    Args:
        session_id: The desktop session ID.
        combo: Key combo (e.g., "Return", "ctrl+a", "alt+F4", "Tab").
    """
    try:
        with _client() as c:
            resp = c.post(f"/session/{session_id}/input", json={
                "action": "key", "combo": combo,
            })
            resp.raise_for_status()
            return f"Key pressed: {combo}"
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"


@server.tool()
def desktop_scroll(
    session_id: str,
    dx: int = 0,
    dy: int = 0,
) -> str:
    """Scroll the desktop VM's virtual display.

    Args:
        session_id: The desktop session ID.
        dx: Horizontal scroll delta (positive = right).
        dy: Vertical scroll delta (positive = down).
    """
    try:
        with _client() as c:
            resp = c.post(f"/session/{session_id}/input", json={
                "action": "scroll", "dx": dx, "dy": dy,
            })
            resp.raise_for_status()
            return f"Scrolled: dx={dx}, dy={dy}"
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"


# ── Browser Control ──


@server.tool()
def browser_navigate(session_id: str, url: str) -> str:
    """Navigate Chromium to a URL in the desktop VM.

    Opens Chromium if not running, then navigates via CDP Page.navigate.

    Args:
        session_id: The desktop session ID.
        url: URL to navigate to.

    Returns:
        Navigation status.
    """
    # Ensure browser is running
    try:
        with _client(timeout=20) as c:
            resp = c.post(f"/session/{session_id}/browser", json={"url": url})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"

    action = data.get("action", "unknown")
    pid = data.get("pid", "unknown")

    if action == "open":
        # Browser just started — it already navigated to the URL
        return f"Browser opened at {url} (PID: {pid})"

    # Browser was already running — use CDP to navigate the current tab
    try:
        with _client(timeout=15) as c:
            resp = c.post(f"/session/{session_id}/browser/control", json={
                "cdp_method": "Page.navigate",
                "cdp_params": {"url": url},
            })
            resp.raise_for_status()
            nav_data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Navigate error: {e.response.status_code} — {e.response.text}"

    frame_id = nav_data.get("frameId", "unknown")
    error = nav_data.get("errorText", "")
    if error:
        return f"Navigation to {url}: error={error}, frameId={frame_id}"
    return f"Navigated to {url} (frameId: {frame_id})"


@server.tool()
def browser_get_text(session_id: str, selector: str = "body") -> str:
    """Extract text content from the current page using CDP.

    Uses Chrome DevTools Protocol to query the DOM and extract text.

    Args:
        session_id: The desktop session ID.
        selector: CSS selector to extract text from (default: "body" for full page).

    Returns:
        Text content from the matching element(s).
    """
    js_code = (
        f"document.querySelector('{selector}')?.innerText || "
        f"'No element found for selector: {selector}'"
    )
    try:
        with _client(timeout=15) as c:
            resp = c.post(f"/session/{session_id}/browser/control", json={
                "cdp_method": "Runtime.evaluate",
                "cdp_params": {"expression": js_code},
            })
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"

    result = data.get("result", {})
    if isinstance(result, dict):
        value = result.get("result", {}).get("value", str(result))
    else:
        value = str(result)
    return f"Text from '{selector}':\n{str(value)[:10000]}"


@server.tool()
def browser_run_js(session_id: str, expression: str) -> str:
    """Execute JavaScript in the desktop VM's browser via CDP.

    Args:
        session_id: The desktop session ID.
        expression: JavaScript expression to evaluate.

    Returns:
        The expression's return value.
    """
    try:
        with _client(timeout=15) as c:
            resp = c.post(f"/session/{session_id}/browser/control", json={
                "cdp_method": "Runtime.evaluate",
                "cdp_params": {"expression": expression},
            })
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"

    result = data.get("result", {})
    if isinstance(result, dict):
        value = result.get("result", {}).get("value", json.dumps(result, indent=2))
    else:
        value = str(result)
    return f"JS result: {str(value)[:10000]}"


@server.tool()
def browser_get_url(session_id: str) -> str:
    """Get the current URL of the browser in the desktop VM.

    Args:
        session_id: The desktop session ID.

    Returns:
        Current page URL and title.
    """
    try:
        with _client(timeout=10) as c:
            resp = c.post(f"/session/{session_id}/browser/control", json={
                "cdp_method": "Runtime.evaluate",
                "cdp_params": {"expression": "JSON.stringify({url: location.href, title: document.title})"},
            })
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"

    result = data.get("result", {})
    if isinstance(result, dict):
        value = result.get("result", {}).get("value", "{}")
    else:
        value = str(result)

    try:
        page_info = json.loads(value)
        return f"URL: {page_info.get('url', 'unknown')}\nTitle: {page_info.get('title', 'unknown')}"
    except (json.JSONDecodeError, TypeError):
        return f"Page info: {value}"


@server.tool()
def desktop_exec(session_id: str, command: str, timeout: int = 30) -> str:
    """Execute a shell command in the desktop VM.

    Args:
        session_id: The desktop session ID.
        command: Shell command to execute.
        timeout: Max execution time in seconds.

    Returns:
        Command output (stdout, stderr, exit code).
    """
    try:
        with _client(timeout=timeout + 10) as c:
            resp = c.post(f"/session/{session_id}/exec", json={
                "command": command, "timeout": timeout,
            })
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Engine error: {e.response.status_code} — {e.response.text}"
    except httpx.ConnectError:
        return "Engine unavailable"

    parts = []
    if data.get("stdout"):
        parts.append(f"STDOUT:\n{data['stdout']}")
    if data.get("stderr"):
        parts.append(f"STDERR:\n{data['stderr']}")
    parts.append(f"Exit code: {data.get('exit_code', 'unknown')}")
    return "\n".join(parts)


if __name__ == "__main__":
    server.run(transport="streamable-http")
