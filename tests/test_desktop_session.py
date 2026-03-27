"""Tests for Tier B desktop VM sessions.

These tests require the desktop disk image at config/vm-images/alpine-desktop.img.
They boot real desktop VMs with Xvfb + Chromium for end-to-end validation.
"""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "sandbox-engine"))

from sandbox_engine import microvm
from sandbox_engine.microvm import MicroVM


def desktop_available() -> bool:
    """Check if desktop image is available for testing."""
    return microvm.is_available("desktop")


skip_no_desktop = pytest.mark.skipif(
    not desktop_available(),
    reason="Desktop image not available",
)


@skip_no_desktop
class TestDesktopBoot:
    """Test desktop VM boot and basic lifecycle."""

    def test_boot_and_ping(self):
        vm = MicroVM(
            cpus=2, memory_gb=2,
            disk_image=str(microvm._DESKTOP_IMAGE),
        )
        try:
            vm.start(timeout=30)
            response = vm.ping()
            result = response["result"]
            assert result["status"] == "ok"
            assert result["hostname"] == "sandbox-desktop"
            assert result["display"] == ":99"
            assert result["xvfb"] is True
        finally:
            vm.stop()

    def test_boot_context_manager(self):
        with MicroVM(
            cpus=2, memory_gb=2,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            result = vm.exec_command("hostname", timeout=5)
            assert result["exit_code"] == 0
            assert "sandbox-desktop" in result["stdout"]

    def test_xvfb_running(self):
        with MicroVM(
            cpus=2, memory_gb=2,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            result = vm.exec_command("pgrep -a Xvfb", timeout=5)
            assert result["exit_code"] == 0
            assert "Xvfb" in result["stdout"]

    def test_display_set(self):
        with MicroVM(
            cpus=2, memory_gb=2,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            result = vm.exec_command("echo $DISPLAY", timeout=5)
            assert result["exit_code"] == 0
            assert ":99" in result["stdout"]

    def test_python3_available(self):
        with MicroVM(
            cpus=2, memory_gb=2,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            result = vm.exec_command("python3 --version", timeout=5)
            assert result["exit_code"] == 0
            assert "Python" in result["stdout"]


@skip_no_desktop
class TestDesktopScreenshot:
    """Test screenshot capture from virtual display."""

    def test_screenshot_png(self):
        with MicroVM(
            cpus=2, memory_gb=2,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            result = vm.screenshot(format="png")
            assert "image_b64" in result
            assert result["format"] == "png"
            assert result["size"] > 0
            # Verify it's valid base64
            raw = base64.b64decode(result["image_b64"])
            assert raw[:4] == b"\x89PNG"

    def test_screenshot_size(self):
        with MicroVM(
            cpus=2, memory_gb=2,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            result = vm.screenshot()
            # Desktop screenshot of 1280x720 should be at least 1KB
            assert result["size"] > 1000


@skip_no_desktop
class TestDesktopInput:
    """Test input injection via xdotool."""

    def test_key_press(self):
        with MicroVM(
            cpus=2, memory_gb=2,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            result = vm.input("key", combo="Return")
            assert result.get("action") == "key"
            assert result.get("combo") == "Return"

    def test_type_text(self):
        with MicroVM(
            cpus=2, memory_gb=2,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            result = vm.input("type", text="hello")
            assert result.get("action") == "type"

    def test_click(self):
        with MicroVM(
            cpus=2, memory_gb=2,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            result = vm.input("click", x=640, y=360, button=1)
            assert result.get("action") == "click"
            assert result.get("x") == 640
            assert result.get("y") == 360

    def test_mousemove(self):
        with MicroVM(
            cpus=2, memory_gb=2,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            result = vm.input("mousemove", x=100, y=200)
            assert result.get("action") == "mousemove"


@skip_no_desktop
class TestDesktopBrowser:
    """Test Chromium browser control."""

    def test_browser_open(self):
        with MicroVM(
            cpus=4, memory_gb=4,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            result = vm.browser_open("about:blank")
            assert "pid" in result
            assert result["pid"] > 0

    def test_browser_screenshot_differs(self):
        """Screenshot should change after browser opens."""
        with MicroVM(
            cpus=4, memory_gb=4,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            # Screenshot before browser
            before = vm.screenshot()
            before_size = before["size"]

            # Open browser
            vm.browser_open("about:blank")

            # Screenshot after — should be different (Chromium window visible)
            import time
            time.sleep(2)
            after = vm.screenshot()
            after_size = after["size"]

            # Browser window should make the screenshot larger (more content)
            assert after_size != before_size

    def test_browser_cdp_evaluate(self):
        """Runtime.evaluate via CDP WebSocket."""
        with MicroVM(
            cpus=4, memory_gb=4,
            disk_image=str(microvm._DESKTOP_IMAGE),
        ) as vm:
            vm.browser_open("about:blank")
            import time
            time.sleep(2)

            result = vm.browser_control(
                "Runtime.evaluate",
                {"expression": "2 + 2"},
            )
            # Result should contain the evaluated value
            assert "result" in result
            value = result.get("result", {})
            if isinstance(value, dict):
                assert value.get("value") == 4 or value.get("type") == "number"


@skip_no_desktop
class TestDesktopSession:
    """Test desktop sessions via the engine API."""

    @pytest.fixture
    def client(self):
        """Create a test client for the engine."""
        from starlette.testclient import TestClient
        from sandbox_engine.server import app
        return TestClient(app)

    def test_create_desktop_session(self, client):
        resp = client.post("/session", json={
            "tier": "B",
            "image": "desktop",
            "memory_gb": 2,
            "cpus": 2,
            "ttl_seconds": 120,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == "B"
        assert data["status"] == "active"
        session_id = data["id"]

        # Clean up
        client.delete(f"/session/{session_id}")

    def test_desktop_session_exec(self, client):
        resp = client.post("/session", json={
            "tier": "B",
            "image": "desktop",
            "memory_gb": 2,
            "cpus": 2,
            "ttl_seconds": 120,
        })
        session_id = resp.json()["id"]

        try:
            # Execute a command
            resp = client.post(f"/session/{session_id}/exec", json={
                "command": "hostname",
                "timeout": 10,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["exit_code"] == 0
            assert "sandbox-desktop" in data["stdout"]
        finally:
            client.delete(f"/session/{session_id}")

    def test_desktop_session_screenshot(self, client):
        resp = client.post("/session", json={
            "tier": "B",
            "image": "desktop",
            "memory_gb": 2,
            "cpus": 2,
            "ttl_seconds": 120,
        })
        session_id = resp.json()["id"]

        try:
            resp = client.post(f"/session/{session_id}/screenshot", json={
                "format": "png",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "image_b64" in data
            assert data["size"] > 0
        finally:
            client.delete(f"/session/{session_id}")

    def test_desktop_session_input(self, client):
        resp = client.post("/session", json={
            "tier": "B",
            "image": "desktop",
            "memory_gb": 2,
            "cpus": 2,
            "ttl_seconds": 120,
        })
        session_id = resp.json()["id"]

        try:
            resp = client.post(f"/session/{session_id}/input", json={
                "action": "key",
                "combo": "Return",
            })
            assert resp.status_code == 200
        finally:
            client.delete(f"/session/{session_id}")

    def test_non_desktop_session_rejects_screenshot(self, client):
        """Non-desktop sessions should reject desktop RPCs."""
        resp = client.post("/session", json={
            "tier": "A",
            "ttl_seconds": 120,
        })
        session_id = resp.json()["id"]

        try:
            resp = client.post(f"/session/{session_id}/screenshot", json={
                "format": "png",
            })
            assert resp.status_code == 400
            assert "desktop" in resp.json()["detail"].lower()
        finally:
            client.delete(f"/session/{session_id}")

    def test_desktop_session_destroy(self, client):
        resp = client.post("/session", json={
            "tier": "B",
            "image": "desktop",
            "memory_gb": 2,
            "cpus": 2,
            "ttl_seconds": 120,
        })
        session_id = resp.json()["id"]

        resp = client.delete(f"/session/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "destroyed"

        # Session should be gone
        resp = client.get(f"/session/{session_id}")
        assert resp.json()["status"] == "destroyed"
