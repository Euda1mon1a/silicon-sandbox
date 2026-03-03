"""Tests for UI server (Phase 7)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

# Add project root and ui dir to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from ui.server import app


client = TestClient(app)


class TestUIHealth:

    @patch("ui.server._check_health")
    def test_health_all_ok(self, mock_check):
        mock_check.return_value = True
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    @patch("ui.server._check_health")
    def test_health_degraded(self, mock_check):
        mock_check.return_value = False
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["engine"] == "down"


class TestUIStatic:

    def test_index_returns_html(self):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "SiliconSandbox" in resp.text

    def test_missing_asset_404(self):
        resp = client.get("/assets/nonexistent.js")
        assert resp.status_code == 404


class TestUIProxy:

    @patch("ui.server.httpx.AsyncClient")
    def test_proxy_engine_get(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        resp = client.get("/api/engine/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @patch("ui.server.httpx.AsyncClient")
    def test_proxy_orchestrator_get(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"tasks": []}
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        resp = client.get("/api/orchestrator/tasks")
        assert resp.status_code == 200

    def test_proxy_engine_unavailable(self):
        # No mock — real connection will fail since engine isn't running
        resp = client.get("/api/engine/health")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["error"]

    def test_proxy_orchestrator_unavailable(self):
        resp = client.get("/api/orchestrator/health")
        assert resp.status_code == 503


class TestIndexContent:

    def test_has_alpine_js(self):
        resp = client.get("/")
        assert "alpinejs" in resp.text

    def test_has_views(self):
        resp = client.get("/")
        assert "tasks" in resp.text
        assert "sandboxes" in resp.text.lower()
        assert "memory" in resp.text.lower()

    def test_has_websocket_connect(self):
        resp = client.get("/")
        assert "WebSocket" in resp.text
