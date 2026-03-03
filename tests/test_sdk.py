"""Tests for the SiliconSandbox Python SDK (Phase 8D)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add SDK to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "sdk"))

from silicon_sandbox import Sandbox, Session
from silicon_sandbox.client import ExecResult, SandboxResult, _SessionContext


class TestSandboxRun:
    """Test Sandbox.run() static method."""

    @patch("silicon_sandbox.client.httpx.Client")
    def test_basic_run(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "id": "abc123",
            "tier": "A",
            "status": "completed",
            "exit_code": 0,
            "stdout": "hello\n",
            "stderr": "",
            "elapsed_seconds": 0.1,
            "violations": [],
            "error": None,
        }
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = Sandbox.run("echo hello")
        assert isinstance(result, SandboxResult)
        assert result.ok
        assert result.stdout == "hello\n"
        assert result.exit_code == 0
        assert result.tier == "A"

    @patch("silicon_sandbox.client.httpx.Client")
    def test_run_with_files(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "id": "xyz789",
            "tier": "A",
            "status": "completed",
            "exit_code": 0,
            "stdout": "42\n",
            "stderr": "",
        }
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = Sandbox.run(
            "python3 script.py",
            files={"script.py": "print(42)"},
        )
        assert result.ok
        # Verify files were included in payload
        call_args = mock_client.post.call_args
        assert "files" in call_args[1]["json"]

    @patch("silicon_sandbox.client.httpx.Client")
    def test_run_failed(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "id": "fail1",
            "tier": "A",
            "status": "failed",
            "exit_code": 1,
            "stdout": "",
            "stderr": "error\n",
        }
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = Sandbox.run("exit 1")
        assert not result.ok
        assert result.exit_code == 1


class TestSandboxCreate:
    """Test Sandbox.create() context manager."""

    @patch("silicon_sandbox.client.httpx.Client")
    def test_context_manager(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "id": "ctx1",
            "tier": "A",
            "status": "completed",
            "exit_code": 0,
            "stdout": "ctx\n",
            "stderr": "",
        }
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        with Sandbox.create("echo ctx") as result:
            assert result.ok
            assert "ctx" in result.stdout


class TestSandboxHealth:
    """Test Sandbox.health()."""

    @patch("silicon_sandbox.client.httpx.Client")
    def test_health(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "version": "0.3.0",
            "sandbox_exec_available": True,
        }
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        health = Sandbox.health()
        assert health["status"] == "ok"


class TestSession:
    """Test Session class."""

    @patch("silicon_sandbox.client.httpx.Client")
    def test_session_exec(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "exit_code": 0,
            "stdout": "session-hello\n",
            "stderr": "",
            "elapsed_seconds": 0.05,
            "violations": [],
        }
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        session = Session(session_id="sess1", tier="A")
        result = session.exec("echo session-hello")
        assert isinstance(result, ExecResult)
        assert result.ok
        assert "session-hello" in result.stdout

    @patch("silicon_sandbox.client.httpx.Client")
    def test_session_write_files(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"written": ["test.py"]}
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        session = Session(session_id="sess2", tier="A")
        written = session.write_files({"test.py": "print(1)"})
        assert "test.py" in written

    @patch("silicon_sandbox.client.httpx.Client")
    def test_session_read_file(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.text = "file contents"
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        session = Session(session_id="sess3", tier="A")
        content = session.read_file("test.txt")
        assert content == "file contents"

    @patch("silicon_sandbox.client.httpx.Client")
    def test_session_info(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "id": "sess4",
            "tier": "A",
            "status": "active",
            "exec_count": 5,
        }
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        session = Session(session_id="sess4", tier="A")
        info = session.info()
        assert info["status"] == "active"
        assert info["exec_count"] == 5

    @patch("silicon_sandbox.client.httpx.Client")
    def test_session_destroy(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "destroyed"}
        mock_client.delete.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        session = Session(session_id="sess5", tier="A")
        session.destroy()
        mock_client.delete.assert_called_once()


class TestSessionContext:
    """Test Session.create() context manager."""

    @patch("silicon_sandbox.client.httpx.Client")
    def test_context_manager_creates_and_destroys(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        # First call: create session
        create_resp = MagicMock()
        create_resp.json.return_value = {
            "id": "ctx-sess1",
            "tier": "A",
            "status": "active",
        }
        # Second call: destroy session
        destroy_resp = MagicMock()
        destroy_resp.json.return_value = {"status": "destroyed"}

        mock_client.post.return_value = create_resp
        mock_client.delete.return_value = destroy_resp
        mock_client_cls.return_value = mock_client

        with Session.create() as session:
            assert session.id == "ctx-sess1"
            assert session.tier == "A"

        # Verify destroy was called
        mock_client.delete.assert_called_once()


class TestExecResult:
    """Test ExecResult dataclass."""

    def test_ok_property(self):
        r = ExecResult(exit_code=0, stdout="ok")
        assert r.ok

    def test_not_ok(self):
        r = ExecResult(exit_code=1, stderr="error")
        assert not r.ok


class TestSandboxResult:
    """Test SandboxResult dataclass."""

    def test_ok_property(self):
        r = SandboxResult(id="t1", tier="A", status="completed", exit_code=0)
        assert r.ok

    def test_not_ok_failed(self):
        r = SandboxResult(id="t2", tier="A", status="failed", exit_code=1)
        assert not r.ok

    def test_not_ok_status(self):
        r = SandboxResult(id="t3", tier="A", status="timeout", exit_code=-1)
        assert not r.ok
