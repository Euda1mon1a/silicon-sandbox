"""Tests for MCP tool servers (Phase 6)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

TOOLS_ROOT = Path(__file__).parent.parent / "tools"


def _load_server(tool_name: str):
    """Load a tool server module by name, avoiding module name collisions."""
    mod_name = f"server_{tool_name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(
        mod_name, TOOLS_ROOT / tool_name / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# Code Interpreter Tests
# ===========================================================================

class TestCodeInterpreter:

    def setup_method(self):
        self.server = _load_server("code-interpreter")

    def test_list_tools(self):
        tools = run(self.server.list_tools())
        names = {t.name for t in tools}
        assert "execute" in names
        assert "upload_file" in names
        assert len(tools) == 2

    def test_execute_tool_schema(self):
        tools = run(self.server.list_tools())
        execute = next(t for t in tools if t.name == "execute")
        props = execute.inputSchema["properties"]
        assert "code" in props
        assert "language" in props
        assert "timeout" in props
        assert "tier" in props
        assert execute.inputSchema["required"] == ["code"]

    def test_shell_quote(self):
        assert self.server._shell_quote("hello") == "'hello'"
        assert self.server._shell_quote("it's") == "'it'\"'\"'s'"

    def test_execute_unsupported_language(self):
        result = run(self.server.call_tool("execute", {"code": "x", "language": "rust"}))
        assert "Unsupported language" in result[0].text

    def test_upload_file(self):
        result = run(self.server.call_tool("upload_file", {
            "filename": "test.py",
            "content": "print('hi')",
            "session_id": "sess1",
        }))
        assert "uploaded" in result[0].text
        assert "sess1" in result[0].text
        assert "sess1" in self.server._sessions

    def test_upload_file_no_filename(self):
        result = run(self.server.call_tool("upload_file", {"filename": "", "content": "x"}))
        assert "required" in result[0].text.lower()

    def test_unknown_tool(self):
        result = run(self.server.call_tool("nonexistent", {}))
        assert "Unknown tool" in result[0].text

    @patch("server_code_interpreter.httpx.AsyncClient")
    def test_execute_python(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stdout": "42\n", "stderr": "", "exit_code": 0, "status": "completed",
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("execute", {"code": "print(42)", "language": "python"}))
        assert "42" in result[0].text
        assert "Exit code: 0" in result[0].text

    @patch("server_code_interpreter.httpx.AsyncClient")
    def test_execute_bash(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stdout": "hello\n", "stderr": "", "exit_code": 0, "status": "completed",
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("execute", {"code": "echo hello", "language": "bash"}))
        assert "hello" in result[0].text

    @patch("server_code_interpreter.httpx.AsyncClient")
    def test_execute_sandbox_error(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("execute", {"code": "x", "language": "python"}))
        assert "Sandbox API error" in result[0].text

    @patch("server_code_interpreter.httpx.AsyncClient")
    def test_session_files_passed_to_sandbox(self, mock_client_cls):
        run(self.server.call_tool("upload_file", {
            "filename": "data.csv", "content": "a,b\n1,2", "session_id": "sess2",
        }))

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"stdout": "", "stderr": "", "exit_code": 0, "status": "completed"}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        run(self.server.call_tool("execute", {
            "code": "import csv", "language": "python", "session_id": "sess2",
        }))

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1].get("json")
        assert payload["files"] == {"data.csv": "a,b\n1,2"}

    @patch("server_code_interpreter.httpx.AsyncClient")
    def test_execute_node(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stdout": "hello\n", "stderr": "", "exit_code": 0, "status": "completed",
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("execute", {"code": "console.log('hello')", "language": "node"}))
        assert "hello" in result[0].text


# ===========================================================================
# File Manager Tests
# ===========================================================================

class TestFileManager:

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.server = _load_server("file-manager")
        self.server.WORKSPACE_ROOT = Path(self.tmpdir)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_list_tools(self):
        tools = run(self.server.list_tools())
        names = {t.name for t in tools}
        assert names == {"read", "write", "list", "search", "delete"}

    def test_write_and_read(self):
        result = run(self.server.call_tool("write", {"path": "hello.txt", "content": "world"}))
        assert "Written" in result[0].text

        result = run(self.server.call_tool("read", {"path": "hello.txt"}))
        assert result[0].text == "world"

    def test_read_nonexistent(self):
        result = run(self.server.call_tool("read", {"path": "nope.txt"}))
        assert "not found" in result[0].text.lower()

    def test_write_nested_dirs(self):
        result = run(self.server.call_tool("write", {"path": "sub/dir/file.txt", "content": "nested"}))
        assert "Written" in result[0].text
        assert (Path(self.tmpdir) / "default" / "sub" / "dir" / "file.txt").exists()

    def test_list_directory(self):
        run(self.server.call_tool("write", {"path": "a.txt", "content": "1"}))
        run(self.server.call_tool("write", {"path": "b.txt", "content": "22"}))
        result = run(self.server.call_tool("list", {}))
        assert "a.txt" in result[0].text
        assert "b.txt" in result[0].text

    def test_list_empty(self):
        (Path(self.tmpdir) / "default").mkdir(parents=True, exist_ok=True)
        result = run(self.server.call_tool("list", {}))
        assert "empty" in result[0].text.lower()

    def test_search_glob(self):
        run(self.server.call_tool("write", {"path": "foo.py", "content": "x"}))
        run(self.server.call_tool("write", {"path": "bar.txt", "content": "y"}))
        result = run(self.server.call_tool("search", {"pattern": "*.py"}))
        assert "foo.py" in result[0].text
        assert "bar.txt" not in result[0].text

    def test_search_no_matches(self):
        (Path(self.tmpdir) / "default").mkdir(parents=True, exist_ok=True)
        result = run(self.server.call_tool("search", {"pattern": "*.xyz"}))
        assert "No files" in result[0].text

    def test_delete(self):
        run(self.server.call_tool("write", {"path": "to_delete.txt", "content": "bye"}))
        result = run(self.server.call_tool("delete", {"path": "to_delete.txt"}))
        assert "Deleted" in result[0].text
        assert not (Path(self.tmpdir) / "default" / "to_delete.txt").exists()

    def test_delete_nonexistent(self):
        result = run(self.server.call_tool("delete", {"path": "nope.txt"}))
        assert "not found" in result[0].text.lower()

    def test_delete_directory_rejected(self):
        (Path(self.tmpdir) / "default" / "subdir").mkdir(parents=True, exist_ok=True)
        result = run(self.server.call_tool("delete", {"path": "subdir"}))
        assert "Cannot delete directories" in result[0].text

    def test_path_traversal_blocked(self):
        result = run(self.server.call_tool("read", {"path": "../../../etc/passwd"}))
        assert "error" in result[0].text.lower() or "traversal" in result[0].text.lower()

    def test_workspace_isolation(self):
        run(self.server.call_tool("write", {"path": "a.txt", "content": "ws1", "workspace": "ws1"}))
        run(self.server.call_tool("write", {"path": "a.txt", "content": "ws2", "workspace": "ws2"}))
        r1 = run(self.server.call_tool("read", {"path": "a.txt", "workspace": "ws1"}))
        r2 = run(self.server.call_tool("read", {"path": "a.txt", "workspace": "ws2"}))
        assert r1[0].text == "ws1"
        assert r2[0].text == "ws2"

    def test_unknown_tool(self):
        result = run(self.server.call_tool("nonexistent", {}))
        assert "Unknown tool" in result[0].text


# ===========================================================================
# Web Research Tests
# ===========================================================================

class TestWebResearch:

    def setup_method(self):
        self.server = _load_server("web-research")

    def test_list_tools(self):
        tools = run(self.server.list_tools())
        names = {t.name for t in tools}
        assert names == {"search", "fetch_page"}

    @patch("server_web_research.DDGS")
    def test_search(self, mock_ddgs_cls):
        mock_ddgs = MagicMock()
        mock_ddgs.text.return_value = [
            {"title": "Result 1", "href": "https://example.com", "body": "Snippet 1"},
            {"title": "Result 2", "href": "https://example.org", "body": "Snippet 2"},
        ]
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs_cls.return_value = mock_ddgs

        result = run(self.server.call_tool("search", {"query": "test query", "num_results": 2}))
        assert "Result 1" in result[0].text
        assert "Result 2" in result[0].text

    @patch("server_web_research.DDGS")
    def test_search_no_results(self, mock_ddgs_cls):
        mock_ddgs = MagicMock()
        mock_ddgs.text.return_value = []
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs_cls.return_value = mock_ddgs

        result = run(self.server.call_tool("search", {"query": "xyzzy"}))
        assert "No results" in result[0].text

    @patch("server_web_research.DDGS")
    def test_search_caps_at_20(self, mock_ddgs_cls):
        mock_ddgs = MagicMock()
        mock_ddgs.text.return_value = []
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs_cls.return_value = mock_ddgs

        run(self.server.call_tool("search", {"query": "test", "num_results": 50}))
        mock_ddgs.text.assert_called_once_with("test", max_results=20)

    @patch("server_web_research.httpx.AsyncClient")
    def test_fetch_page(self, mock_client_cls):
        html = "<html><head><title>Test Page</title></head><body><p>Hello world content</p></body></html>"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("fetch_page", {"url": "https://example.com"}))
        assert "Test Page" in result[0].text or "Hello" in result[0].text

    @patch("server_web_research.httpx.AsyncClient")
    def test_fetch_page_http_error(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("fetch_page", {"url": "https://example.com/nope"}))
        assert "404" in result[0].text

    @patch("server_web_research.httpx.AsyncClient")
    def test_fetch_page_unsupported_type(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/pdf"}
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("fetch_page", {"url": "https://example.com/doc.pdf"}))
        assert "Unsupported" in result[0].text

    @patch("server_web_research.httpx.AsyncClient")
    def test_fetch_page_truncation(self, mock_client_cls):
        long_body = "<html><body>" + "x" * 20000 + "</body></html>"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = long_body
        mock_resp.headers = {"content-type": "text/html"}
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("fetch_page", {"url": "https://example.com", "max_length": 100}))
        assert "truncated" in result[0].text

    def test_unknown_tool(self):
        result = run(self.server.call_tool("nonexistent", {}))
        assert "Unknown tool" in result[0].text


# ===========================================================================
# Browser Automation Tests
# ===========================================================================

class TestBrowserAutomation:

    def setup_method(self):
        self.server = _load_server("browser-automation")

    def test_list_tools(self):
        tools = run(self.server.list_tools())
        names = {t.name for t in tools}
        assert names == {"navigate", "screenshot", "extract_text"}

    def test_extract_domain(self):
        assert self.server._extract_domain("https://example.com/path") == "example.com"
        assert self.server._extract_domain("http://sub.domain.org:8080/x") == "sub.domain.org"
        assert self.server._extract_domain("invalid") == ""

    def test_screenshot_stub(self):
        result = run(self.server.call_tool("screenshot", {"url": "https://example.com"}))
        assert "checkpoint" in result[0].text.lower()
        assert "playwright" in result[0].text.lower()

    @patch("server_browser_automation.httpx.AsyncClient")
    def test_navigate_success(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stdout": "<html><body>Hello World</body></html>", "stderr": "", "exit_code": 0,
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("navigate", {"url": "https://example.com"}))
        assert "Hello World" in result[0].text

    @patch("server_browser_automation.httpx.AsyncClient")
    def test_navigate_strips_html(self, mock_client_cls):
        html = "<html><head><script>evil()</script><style>body{}</style></head><body><p>Clean text</p></body></html>"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"stdout": html, "stderr": "", "exit_code": 0}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("navigate", {"url": "https://example.com"}))
        assert "Clean text" in result[0].text
        assert "<script>" not in result[0].text

    @patch("server_browser_automation.httpx.AsyncClient")
    def test_navigate_sandbox_error(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("navigate", {"url": "https://example.com"}))
        assert "Sandbox error" in result[0].text

    @patch("server_browser_automation.httpx.AsyncClient")
    def test_navigate_empty_content(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"stdout": "", "stderr": "", "exit_code": 0}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("navigate", {"url": "https://example.com"}))
        assert "No content" in result[0].text

    @patch("server_browser_automation.httpx.AsyncClient")
    def test_navigate_uses_tier_b(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"stdout": "hi", "exit_code": 0}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        run(self.server.call_tool("navigate", {"url": "https://example.com"}))

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1].get("json")
        assert payload["tier"] == "B"
        assert payload["allow_network"] is True
        assert "example.com" in payload["allowed_domains"]

    @patch("server_browser_automation.httpx.AsyncClient")
    def test_extract_text(self, mock_client_cls):
        html = "<html><head><title>Article</title></head><body><article><p>Main content here.</p></article></body></html>"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("extract_text", {"url": "https://example.com/article"}))
        assert len(result) == 1

    @patch("server_browser_automation.httpx.AsyncClient")
    def test_extract_text_http_error(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = run(self.server.call_tool("extract_text", {"url": "https://example.com"}))
        assert "403" in result[0].text

    def test_unknown_tool(self):
        result = run(self.server.call_tool("nonexistent", {}))
        assert "Unknown tool" in result[0].text


# === SiliconSandbox MCP Server Tests (Phase 9) ===


class TestSandboxMCPServer:
    """Tests for the SiliconSandbox MCP server (sandbox-mcp/server.py)."""

    @classmethod
    def setup_class(cls):
        cls.mod = _load_server("sandbox-mcp")

    def test_module_loads(self):
        assert hasattr(self.mod, "server")
        assert hasattr(self.mod, "sandbox_run")
        assert hasattr(self.mod, "sandbox_health")
        assert hasattr(self.mod, "session_create")
        assert hasattr(self.mod, "session_exec")
        assert hasattr(self.mod, "session_write_files")
        assert hasattr(self.mod, "session_read_file")
        assert hasattr(self.mod, "session_list")
        assert hasattr(self.mod, "session_destroy")
        assert hasattr(self.mod, "session_pause")
        assert hasattr(self.mod, "session_resume")

    @patch("server_sandbox_mcp.httpx.Client")
    def test_sandbox_run_success(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "abc123",
            "tier": "A",
            "status": "completed",
            "exit_code": 0,
            "stdout": "hello\n",
            "stderr": "",
            "elapsed_seconds": 0.5,
            "violations": [],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = self.mod.sandbox_run("echo hello")
        assert "STDOUT:" in result
        assert "hello" in result
        assert "Exit code: 0" in result

    @patch("server_sandbox_mcp.httpx.Client")
    def test_sandbox_run_python_language(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "abc123",
            "tier": "A",
            "status": "completed",
            "exit_code": 0,
            "stdout": "42\n",
            "stderr": "",
            "elapsed_seconds": 0.3,
            "violations": [],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = self.mod.sandbox_run("print(42)", language="python")
        assert "42" in result
        # Verify python3 -c was used
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        if isinstance(payload, dict):
            assert "python3 -c" in payload.get("command", "")

    @patch("server_sandbox_mcp.httpx.Client")
    def test_sandbox_run_engine_unavailable(self, mock_client_cls):
        import httpx as _httpx
        mock_client = MagicMock()
        mock_client.post.side_effect = _httpx.ConnectError("Connection refused")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = self.mod.sandbox_run("echo test")
        assert "unavailable" in result.lower()

    @patch("server_sandbox_mcp.httpx.Client")
    def test_sandbox_health(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "version": "0.4.0",
            "sandbox_exec_available": True,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = self.mod.sandbox_health()
        assert "ok" in result
        assert "0.4.0" in result

    @patch("server_sandbox_mcp.httpx.Client")
    def test_session_create(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "sess123",
            "tier": "A",
            "status": "active",
            "ttl_seconds": 3600,
            "workspace": "/tmp/sandbox-test",
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = self.mod.session_create()
        assert "sess123" in result
        assert "active" in result.lower()

    @patch("server_sandbox_mcp.httpx.Client")
    def test_session_exec(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "exit_code": 0,
            "stdout": "hello\n",
            "stderr": "",
            "elapsed_seconds": 0.1,
            "violations": [],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = self.mod.session_exec("sess123", "echo hello")
        assert "hello" in result
        assert "Exit code: 0" in result

    @patch("server_sandbox_mcp.httpx.Client")
    def test_session_list_empty(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = self.mod.session_list()
        assert "No sessions" in result

    @patch("server_sandbox_mcp.httpx.Client")
    def test_session_destroy(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "destroyed", "id": "sess123"}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.delete.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = self.mod.session_destroy("sess123")
        assert "destroyed" in result.lower()

    @patch("server_sandbox_mcp.httpx.Client")
    def test_session_pause(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "paused", "id": "sess123", "stopped_pids": 0}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = self.mod.session_pause("sess123")
        assert "paused" in result.lower()

    @patch("server_sandbox_mcp.httpx.Client")
    def test_session_resume(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "resumed", "id": "sess123", "resumed_pids": 0}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = self.mod.session_resume("sess123")
        assert "resumed" in result.lower()

    def test_shell_quote(self):
        assert self.mod._shell_quote("hello") == "'hello'"
        assert self.mod._shell_quote("it's") == "'it'\"'\"'s'"

    def test_auth_header_included(self):
        """Auth token should be included in headers if configured."""
        original_token = self.mod._auth_token
        try:
            self.mod._auth_token = "test-token-123"
            headers = self.mod._headers()
            assert headers["Authorization"] == "Bearer test-token-123"
        finally:
            self.mod._auth_token = original_token

    def test_no_auth_header_by_default(self):
        """No auth header when token not configured."""
        original_token = self.mod._auth_token
        try:
            self.mod._auth_token = None
            headers = self.mod._headers()
            assert "Authorization" not in headers
        finally:
            self.mod._auth_token = original_token
