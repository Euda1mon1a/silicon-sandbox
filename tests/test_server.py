"""Tests for the Sandbox Engine FastAPI server."""

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "sandbox-engine"))

from sandbox_engine.server import app


@pytest.fixture
def client():
    return TestClient(app)


class TestHealth:
    def test_health_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["version"] == "0.4.0"
        assert "sandbox_exec_available" in data
        assert "virtualization_available" in data
        assert data["virtualization_available"] is True
        assert "proxy_running" in data

    def test_metrics_returns_structure(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200
        data = r.json()
        assert "active_sandboxes" in data
        assert "vm_memory_allocated_gb" in data
        assert "total_violations" in data


class TestSandboxAPI:
    def test_create_tier_a_sandbox(self, client):
        r = client.post("/sandbox", json={
            "tier": "A",
            "command": "echo hello",
            "timeout": 10,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "completed"
        assert data["exit_code"] == 0
        assert "hello" in data["stdout"]

    def test_create_tier_c_sandbox(self, client):
        r = client.post("/sandbox", json={
            "tier": "C",
            "command": "echo native",
            "timeout": 10,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "completed"
        assert data["exit_code"] == 0
        assert "native" in data["stdout"]

    def test_create_tier_b_sandbox(self, client):
        """Tier B — MicroVM execution."""
        r = client.post("/sandbox", json={
            "tier": "B",
            "command": "echo microvm",
            "timeout": 15,
            "memory_gb": 1,
            "cpus": 1,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "completed"
        assert data["exit_code"] == 0
        assert "microvm" in data["stdout"]
        assert data["tier"] == "B"

    def test_auto_tier_selects_a(self, client):
        r = client.post("/sandbox", json={
            "tier": "auto",
            "command": "echo auto",
            "timeout": 10,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["tier"] == "A"
        assert data["status"] == "completed"

    def test_sandbox_with_files(self, client):
        r = client.post("/sandbox", json={
            "tier": "A",
            "command": "cat hello.txt",
            "timeout": 10,
            "files": {"hello.txt": "world"},
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "completed"
        assert "world" in data["stdout"]

    def test_sandbox_timeout(self, client):
        r = client.post("/sandbox", json={
            "tier": "A",
            "command": "sleep 30",
            "timeout": 2,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "timeout"

    def test_get_sandbox_not_found(self, client):
        r = client.get("/sandbox/nonexistent")
        assert r.status_code == 404

    def test_list_sandboxes(self, client):
        # Create one sandbox first
        client.post("/sandbox", json={
            "tier": "A",
            "command": "echo test",
            "timeout": 10,
        })
        r = client.get("/sandboxes")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_auto_tier_needs_linux(self, client):
        """AUTO + needs_linux selects Tier B."""
        r = client.post("/sandbox", json={
            "tier": "auto",
            "command": "uname",
            "timeout": 15,
            "needs_linux": True,
            "memory_gb": 1,
            "cpus": 1,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["tier"] == "B"
        assert data["status"] == "completed"

    def test_kill_sandbox(self, client):
        """Kill a completed sandbox."""
        r = client.post("/sandbox", json={
            "tier": "A",
            "command": "echo kill-me",
            "timeout": 10,
        })
        sandbox_id = r.json()["id"]
        r = client.delete(f"/sandbox/{sandbox_id}")
        assert r.status_code == 200
        assert r.json()["status"] == "killed"


class TestAsyncExecution:
    """Test async sandbox execution mode."""

    def test_async_returns_immediately(self, client):
        """Async mode returns pending/running status without blocking."""
        r = client.post("/sandbox", json={
            "tier": "A",
            "command": "sleep 2 && echo done",
            "timeout": 10,
            "async": True,
        })
        assert r.status_code == 200
        data = r.json()
        # Should return immediately with pending or running status
        assert data["status"] in ("pending", "running")
        assert data["id"]

    def test_async_poll_for_result(self, client):
        """Create async sandbox, poll until complete."""
        r = client.post("/sandbox", json={
            "tier": "A",
            "command": "echo async-result",
            "timeout": 10,
            "async": True,
        })
        assert r.status_code == 200
        sandbox_id = r.json()["id"]

        # Poll until done (should be fast for echo)
        for _ in range(20):
            r = client.get(f"/sandbox/{sandbox_id}")
            data = r.json()
            if data["status"] not in ("pending", "running"):
                break
            time.sleep(0.5)

        assert data["status"] == "completed"
        assert data["exit_code"] == 0
        assert "async-result" in data["stdout"]

    def test_async_wait_endpoint(self, client):
        """POST /sandbox/{id}/wait blocks until completion."""
        r = client.post("/sandbox", json={
            "tier": "A",
            "command": "echo waited",
            "timeout": 10,
            "async": True,
        })
        sandbox_id = r.json()["id"]

        # Wait endpoint should block until complete and return result
        r = client.post(f"/sandbox/{sandbox_id}/wait")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "completed"
        assert "waited" in data["stdout"]

    def test_async_tier_b(self, client):
        """Async mode works with Tier B (MicroVM)."""
        r = client.post("/sandbox", json={
            "tier": "B",
            "command": "echo async-vm",
            "timeout": 15,
            "memory_gb": 1,
            "cpus": 1,
            "async": True,
        })
        assert r.status_code == 200
        sandbox_id = r.json()["id"]

        # Wait for completion
        r = client.post(f"/sandbox/{sandbox_id}/wait")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "completed"
        assert "async-vm" in data["stdout"]
        assert data["tier"] == "B"


class TestWebSocketStream:
    """Test WebSocket streaming endpoint."""

    def test_stream_completed_sandbox(self, client):
        """Streaming a completed sandbox returns result immediately."""
        # Create sync sandbox first
        r = client.post("/sandbox", json={
            "tier": "A",
            "command": "echo ws-test",
            "timeout": 10,
        })
        sandbox_id = r.json()["id"]

        # Connect WebSocket — should get result and close
        with client.websocket_connect(f"/sandbox/{sandbox_id}/stream") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "result"
            assert msg["data"]["status"] == "completed"
            assert "ws-test" in msg["data"]["stdout"]

    def test_stream_async_sandbox(self, client):
        """Stream an async sandbox and receive status + final result."""
        r = client.post("/sandbox", json={
            "tier": "A",
            "command": "echo streamed",
            "timeout": 10,
            "async": True,
        })
        sandbox_id = r.json()["id"]

        with client.websocket_connect(f"/sandbox/{sandbox_id}/stream") as ws:
            messages = []
            for _ in range(20):
                msg = ws.receive_json()
                messages.append(msg)
                if msg["type"] == "result":
                    break

            # Should have at least a status message and a result
            types = [m["type"] for m in messages]
            assert "result" in types
            result_msg = next(m for m in messages if m["type"] == "result")
            assert result_msg["data"]["status"] == "completed"
            assert "streamed" in result_msg["data"]["stdout"]

    def test_stream_nonexistent_sandbox(self, client):
        """WebSocket to nonexistent sandbox closes with 4004."""
        with pytest.raises(Exception):
            with client.websocket_connect("/sandbox/nonexistent/stream") as ws:
                ws.receive_json()


class TestSessions:
    """Test persistent sandbox sessions (Phase 8C)."""

    def test_create_session(self, client):
        """Create a persistent session."""
        r = client.post("/session", json={
            "tier": "A",
            "ttl_seconds": 300,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "active"
        assert data["tier"] == "A"
        assert data["exec_count"] == 0
        assert data["ttl_seconds"] == 300
        # Cleanup
        client.delete(f"/session/{data['id']}")

    def test_session_exec(self, client):
        """Execute commands in a persistent session."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]

        # First exec
        r = client.post(f"/session/{session_id}/exec", json={
            "command": 'echo "hello" > greeting.txt',
            "timeout": 10,
        })
        assert r.status_code == 200
        assert r.json()["exit_code"] == 0

        # Second exec — file persists from first
        r = client.post(f"/session/{session_id}/exec", json={
            "command": "cat greeting.txt",
            "timeout": 10,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["exit_code"] == 0
        assert "hello" in data["stdout"]

        # Cleanup
        client.delete(f"/session/{session_id}")

    def test_session_exec_count(self, client):
        """Exec count increments with each call."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]

        for i in range(3):
            client.post(f"/session/{session_id}/exec", json={
                "command": f"echo {i}",
                "timeout": 5,
            })

        r = client.get(f"/session/{session_id}")
        assert r.json()["exec_count"] == 3

        client.delete(f"/session/{session_id}")

    def test_session_write_and_read_files(self, client):
        """Write and read files via session API."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]

        # Write files
        r = client.post(f"/session/{session_id}/files", json={
            "files": {
                "script.py": "print('hello from session')",
                "data/input.txt": "test data",
            },
        })
        assert r.status_code == 200
        assert "script.py" in r.json()["written"]
        assert "data/input.txt" in r.json()["written"]

        # Read file
        r = client.get(f"/session/{session_id}/files/script.py")
        assert r.status_code == 200
        assert "print('hello from session')" in r.text

        # Read nested file
        r = client.get(f"/session/{session_id}/files/data/input.txt")
        assert r.status_code == 200
        assert "test data" in r.text

        # Execute the written script
        r = client.post(f"/session/{session_id}/exec", json={
            "command": "python3 script.py",
            "timeout": 10,
        })
        assert r.json()["exit_code"] == 0
        assert "hello from session" in r.json()["stdout"]

        client.delete(f"/session/{session_id}")

    def test_session_file_not_found(self, client):
        """Reading a nonexistent file returns 404."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]

        r = client.get(f"/session/{session_id}/files/nonexistent.txt")
        assert r.status_code == 404

        client.delete(f"/session/{session_id}")

    def test_session_path_traversal_blocked(self, client):
        """Path traversal attempts are blocked."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]

        # Write with path traversal — server rejects ".." in filenames
        r = client.post(f"/session/{session_id}/files", json={
            "files": {"../../../etc/passwd": "malicious"},
        })
        assert r.status_code == 400

        # Write with absolute path — server rejects absolute paths
        r = client.post(f"/session/{session_id}/files", json={
            "files": {"/etc/passwd": "malicious"},
        })
        assert r.status_code == 400

        client.delete(f"/session/{session_id}")

    def test_session_destroy(self, client):
        """Destroying a session cleans up the workspace."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]
        workspace = r.json()["workspace"]

        r = client.delete(f"/session/{session_id}")
        assert r.status_code == 200
        assert r.json()["status"] == "destroyed"

        # Session is now destroyed
        r = client.get(f"/session/{session_id}")
        assert r.json()["status"] == "destroyed"

        # Can't exec on destroyed session
        r = client.post(f"/session/{session_id}/exec", json={
            "command": "echo nope",
            "timeout": 5,
        })
        assert r.status_code == 404

    def test_session_not_found(self, client):
        """Accessing nonexistent session returns 404."""
        r = client.get("/session/nonexistent")
        assert r.status_code == 404

    def test_list_sessions(self, client):
        """List all sessions."""
        # Create two sessions
        r1 = client.post("/session", json={"tier": "A"})
        r2 = client.post("/session", json={"tier": "C"})
        id1 = r1.json()["id"]
        id2 = r2.json()["id"]

        r = client.get("/sessions")
        assert r.status_code == 200
        sessions = r.json()
        active_ids = [s["id"] for s in sessions if s["status"] == "active"]
        assert id1 in active_ids
        assert id2 in active_ids

        client.delete(f"/session/{id1}")
        client.delete(f"/session/{id2}")

    def test_session_tier_c(self, client):
        """Sessions work with Tier C (native)."""
        r = client.post("/session", json={"tier": "C"})
        session_id = r.json()["id"]
        assert r.json()["tier"] == "C"

        r = client.post(f"/session/{session_id}/exec", json={
            "command": "echo tier-c-session",
            "timeout": 5,
        })
        assert r.json()["exit_code"] == 0
        assert "tier-c-session" in r.json()["stdout"]

        client.delete(f"/session/{session_id}")

    def test_session_with_initial_files(self, client):
        """Sessions can be created with initial files."""
        r = client.post("/session", json={
            "tier": "A",
            "files": {"config.json": '{"key": "value"}'},
        })
        session_id = r.json()["id"]

        r = client.post(f"/session/{session_id}/exec", json={
            "command": "cat config.json",
            "timeout": 5,
        })
        assert r.json()["exit_code"] == 0
        assert '"key": "value"' in r.json()["stdout"]

        client.delete(f"/session/{session_id}")


class TestSSEStreaming:
    """Test SSE streaming for session exec (Phase 8E)."""

    def test_sse_stream_basic(self, client):
        """SSE streaming returns stdout events and exit event."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]

        # Use the streaming endpoint
        r = client.post(
            f"/session/{session_id}/exec/stream",
            json={"command": "echo line1; echo line2; echo line3", "timeout": 10},
        )
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")

        # Parse SSE events from response body
        events = _parse_sse(r.text)
        stdout_events = [e for e in events if e["event"] == "stdout"]
        exit_events = [e for e in events if e["event"] == "exit"]

        assert len(stdout_events) >= 3
        assert any("line1" in e["data"] for e in stdout_events)
        assert any("line2" in e["data"] for e in stdout_events)
        assert any("line3" in e["data"] for e in stdout_events)
        assert len(exit_events) == 1
        assert '"exit_code": 0' in exit_events[0]["data"]

        client.delete(f"/session/{session_id}")

    def test_sse_stream_stderr(self, client):
        """SSE streaming includes stderr events."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]

        r = client.post(
            f"/session/{session_id}/exec/stream",
            json={"command": "echo out; echo err >&2", "timeout": 10},
        )
        events = _parse_sse(r.text)
        stdout_events = [e for e in events if e["event"] == "stdout"]
        stderr_events = [e for e in events if e["event"] == "stderr"]

        assert any("out" in e["data"] for e in stdout_events)
        assert any("err" in e["data"] for e in stderr_events)

        client.delete(f"/session/{session_id}")

    def test_sse_stream_nonexistent_session(self, client):
        """SSE on nonexistent session returns 404."""
        r = client.post(
            "/session/nonexistent/exec/stream",
            json={"command": "echo nope", "timeout": 5},
        )
        assert r.status_code == 404

    def test_sse_stream_exit_code(self, client):
        """SSE reports correct exit code on failure."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]

        r = client.post(
            f"/session/{session_id}/exec/stream",
            json={"command": "exit 42", "timeout": 10},
        )
        events = _parse_sse(r.text)
        exit_events = [e for e in events if e["event"] == "exit"]
        assert len(exit_events) == 1
        assert '"exit_code": 42' in exit_events[0]["data"]

        client.delete(f"/session/{session_id}")


class TestBearerAuth:
    """Test bearer token authentication (Phase 8F)."""

    def test_no_auth_by_default(self, client):
        """Without auth token configured, all requests pass."""
        # Health is always public
        r = client.get("/health")
        assert r.status_code == 200

        # Sandbox creation works without token
        r = client.post("/sandbox", json={
            "tier": "A",
            "command": "echo no-auth",
            "timeout": 5,
        })
        assert r.status_code == 200

    def test_auth_enforced_when_configured(self):
        """When auth token is set, protected endpoints require it."""
        import sandbox_engine.server as srv
        original_token = srv._auth_token

        try:
            srv._auth_token = "test-secret-token"
            client = TestClient(app)

            # Health stays public
            r = client.get("/health")
            assert r.status_code == 200

            # Metrics stays public
            r = client.get("/metrics")
            assert r.status_code == 200

            # Sandbox list stays public
            r = client.get("/sandboxes")
            assert r.status_code == 200

            # Session list stays public
            r = client.get("/sessions")
            assert r.status_code == 200

            # Sandbox creation requires token
            r = client.post("/sandbox", json={
                "tier": "A",
                "command": "echo nope",
                "timeout": 5,
            })
            assert r.status_code == 401

            # Session creation requires token
            r = client.post("/session", json={"tier": "A"})
            assert r.status_code == 401

        finally:
            srv._auth_token = original_token

    def test_auth_with_valid_token(self):
        """Valid bearer token grants access."""
        import sandbox_engine.server as srv
        original_token = srv._auth_token

        try:
            srv._auth_token = "test-secret-token"
            client = TestClient(app)

            r = client.post(
                "/sandbox",
                json={"tier": "A", "command": "echo authed", "timeout": 5},
                headers={"Authorization": "Bearer test-secret-token"},
            )
            assert r.status_code == 200
            assert r.json()["status"] == "completed"

        finally:
            srv._auth_token = original_token

    def test_auth_with_wrong_token(self):
        """Wrong bearer token is rejected."""
        import sandbox_engine.server as srv
        original_token = srv._auth_token

        try:
            srv._auth_token = "correct-token"
            client = TestClient(app)

            r = client.post(
                "/sandbox",
                json={"tier": "A", "command": "echo nope", "timeout": 5},
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert r.status_code == 401

        finally:
            srv._auth_token = original_token


class TestPauseResume:
    """Test session pause/resume (Phase 9)."""

    def test_pause_and_resume(self, client):
        """Pause sets status to paused, resume sets it back to active."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]
        assert r.json()["status"] == "active"
        assert r.json()["paused"] is False

        # Pause
        r = client.post(f"/session/{session_id}/pause")
        assert r.status_code == 200
        assert r.json()["status"] == "paused"

        # Check session info reflects paused state
        r = client.get(f"/session/{session_id}")
        assert r.json()["status"] == "paused"
        assert r.json()["paused"] is True

        # Resume
        r = client.post(f"/session/{session_id}/resume")
        assert r.status_code == 200
        assert r.json()["status"] == "resumed"

        # Check session info reflects active state
        r = client.get(f"/session/{session_id}")
        assert r.json()["status"] == "active"
        assert r.json()["paused"] is False

        client.delete(f"/session/{session_id}")

    def test_pause_already_paused(self, client):
        """Pausing an already paused session returns already_paused."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]

        client.post(f"/session/{session_id}/pause")
        r = client.post(f"/session/{session_id}/pause")
        assert r.json()["status"] == "already_paused"

        client.delete(f"/session/{session_id}")

    def test_resume_not_paused(self, client):
        """Resuming a non-paused session returns already_running."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]

        r = client.post(f"/session/{session_id}/resume")
        assert r.json()["status"] == "already_running"

        client.delete(f"/session/{session_id}")

    def test_exec_blocked_while_paused(self, client):
        """Exec on a paused session returns 409 Conflict."""
        r = client.post("/session", json={"tier": "A"})
        session_id = r.json()["id"]

        client.post(f"/session/{session_id}/pause")

        r = client.post(f"/session/{session_id}/exec", json={
            "command": "echo nope",
            "timeout": 5,
        })
        assert r.status_code == 409

        # After resume, exec works
        client.post(f"/session/{session_id}/resume")
        r = client.post(f"/session/{session_id}/exec", json={
            "command": "echo works",
            "timeout": 5,
        })
        assert r.status_code == 200
        assert "works" in r.json()["stdout"]

        client.delete(f"/session/{session_id}")

    def test_pause_nonexistent_session(self, client):
        """Pausing nonexistent session returns 404."""
        r = client.post("/session/nonexistent/pause")
        assert r.status_code == 404

    def test_resume_nonexistent_session(self, client):
        """Resuming nonexistent session returns 404."""
        r = client.post("/session/nonexistent/resume")
        assert r.status_code == 404


def _parse_sse(text: str) -> list[dict]:
    """Parse Server-Sent Events text into a list of {event, data} dicts."""
    events = []
    current_event = None
    current_data = []

    for line in text.splitlines():
        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current_data.append(line[len("data:"):].strip())
        elif line == "" and current_event is not None:
            events.append({
                "event": current_event,
                "data": "\n".join(current_data),
            })
            current_event = None
            current_data = []

    # Handle last event if no trailing blank line
    if current_event is not None and current_data:
        events.append({
            "event": current_event,
            "data": "\n".join(current_data),
        })

    return events
