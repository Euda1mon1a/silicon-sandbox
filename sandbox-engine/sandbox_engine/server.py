"""SiliconSandbox Engine — FastAPI server managing three tiers of execution isolation."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import secrets
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sse_starlette.sse import EventSourceResponse

from . import seatbelt, native, microvm
from .models import (
    CreateSandboxRequest,
    CreateSessionRequest,
    HealthResponse,
    SandboxInfo,
    SandboxResult,
    SandboxState,
    SandboxStatus,
    SandboxTier,
    SessionExecRequest,
    SessionExecResult,
    SessionInfo,
    SessionState,
    SessionWriteRequest,
)
from .monitor import SandboxMonitor
from .proxy import AllowlistProxy

logger = logging.getLogger(__name__)

# Global state
_sandboxes: dict[str, SandboxState] = {}
_sessions: dict[str, SessionState] = {}
_monitor = SandboxMonitor()
_proxy: AllowlistProxy | None = None
_config: dict = {}
_background_threads: dict[str, threading.Thread] = {}  # sandbox_id -> running thread
_sandbox_events: dict[str, threading.Event] = {}  # sandbox_id -> completion event
_session_reaper: threading.Thread | None = None
_reaper_stop = threading.Event()

MAX_CONCURRENT_SESSIONS = 10

# Auth: optional bearer token. If set in config, all mutating endpoints require it.
# Health/metrics endpoints are always public.
_auth_token: str | None = None
_bearer_scheme = HTTPBearer(auto_error=False)


async def verify_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Verify bearer token if auth is configured."""
    if _auth_token is None:
        return  # Auth not configured, allow all
    if credentials is None or not secrets.compare_digest(credentials.credentials, _auth_token):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


def load_config() -> dict:
    """Load config from default.yaml."""
    config_paths = [
        Path(__file__).parent.parent.parent / "config" / "default.yaml",
        Path.home() / "workspace" / "silicon-sandbox" / "config" / "default.yaml",
    ]
    for p in config_paths:
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f)
    logger.warning("No config file found, using defaults")
    return {}


def _session_reaper_loop():
    """Background thread that cleans up expired/idle sessions."""
    while not _reaper_stop.is_set():
        try:
            for session_id, session in list(_sessions.items()):
                if not session.active:
                    continue
                if session.is_expired():
                    logger.info("Session %s expired (TTL %ds)", session_id, session.ttl_seconds)
                    _destroy_session(session_id)
                elif session.is_idle():
                    logger.info("Session %s idle timeout", session_id)
                    _destroy_session(session_id)
        except Exception:
            logger.exception("Session reaper error")
        _reaper_stop.wait(60.0)  # Check every 60s


def _destroy_session(session_id: str) -> bool:
    """Destroy a session and clean up its workspace."""
    session = _sessions.get(session_id)
    if not session or not session.active:
        return False
    session.active = False
    if session.workspace and session.workspace.exists():
        seatbelt.destroy_workspace(session.workspace)
    if _proxy:
        _proxy.remove_sandbox_domains(session_id)
    logger.info("Destroyed session %s (exec_count=%d)", session_id, session.exec_count)
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    global _config, _monitor, _proxy, _session_reaper, _auth_token

    _config = load_config()

    # Auth token from config or environment
    _auth_token = (
        os.environ.get("SILICONSANDBOX_AUTH_TOKEN")
        or _config.get("engine", {}).get("auth_token")
    )
    if _auth_token:
        logger.info("Bearer token auth enabled")
    else:
        logger.info("No auth token configured — all requests allowed")

    # Configure monitor
    vm_cap = _config.get("sandbox", {}).get("microvm", {}).get("max_total_memory_gb", 10.0)
    _monitor = SandboxMonitor(vm_memory_cap_gb=vm_cap)

    # Start network allowlist proxy
    net_config = _config.get("sandbox", {}).get("network", {})
    proxy_port = net_config.get("proxy_port", 8098)
    allowed_domains = net_config.get("allowed_domains", [])
    deny_all = net_config.get("deny_all_by_default", True)

    _proxy = AllowlistProxy(
        port=proxy_port,
        allowed_domains=allowed_domains,
        deny_all=deny_all,
    )
    _proxy.start()

    # Start session reaper
    _reaper_stop.clear()
    _session_reaper = threading.Thread(target=_session_reaper_loop, daemon=True)
    _session_reaper.start()

    # Check available tiers
    sb_available = seatbelt.is_available()
    vm_available = microvm.is_available()

    logger.info("SiliconSandbox Engine starting")
    logger.info("  Tier A (Seatbelt): %s", "available" if sb_available else "NOT AVAILABLE")
    logger.info("  Tier B (MicroVM): %s", "available" if vm_available else "NOT AVAILABLE")
    logger.info("  Tier C (Native): available")
    logger.info("  VM memory cap: %.1f GB", vm_cap)
    logger.info("  Network proxy: %s:%d (%d domains)", _proxy.host, _proxy.port, len(_proxy.allowed_domains))

    if not sb_available:
        logger.warning(
            "sandbox-exec not found at /usr/bin/sandbox-exec. "
            "Tier A unavailable — all sandboxes will use Tier C (native process)."
        )

    yield

    # Stop session reaper
    _reaper_stop.set()
    if _session_reaper and _session_reaper.is_alive():
        _session_reaper.join(timeout=2.0)

    # Shutdown proxy
    if _proxy:
        _proxy.stop()

    # Wait for background threads to finish (with timeout)
    for sandbox_id, thread in list(_background_threads.items()):
        if thread.is_alive():
            thread.join(timeout=2.0)
    _background_threads.clear()

    # Signal all completion events
    for event in _sandbox_events.values():
        event.set()
    _sandbox_events.clear()

    # Shutdown: kill all active sandboxes
    for sandbox_id, state in list(_sandboxes.items()):
        if state.status == SandboxStatus.RUNNING:
            logger.info("Shutdown: cleaning up sandbox %s", sandbox_id)
            if state.workspace and state.workspace.exists():
                seatbelt.destroy_workspace(state.workspace)
            state.status = SandboxStatus.KILLED

    # Shutdown: destroy all active sessions
    for session_id in list(_sessions.keys()):
        _destroy_session(session_id)


app = FastAPI(
    title="SiliconSandbox Engine",
    version="0.4.0",
    lifespan=lifespan,
)


def _resolve_tier(requested: SandboxTier, needs_linux: bool = False) -> SandboxTier:
    """Resolve AUTO tier and handle graceful degradation."""
    if requested == SandboxTier.AUTO:
        if needs_linux and microvm.is_available():
            return SandboxTier.B
        elif seatbelt.is_available():
            return SandboxTier.A
        else:
            return SandboxTier.C

    if requested == SandboxTier.A and not seatbelt.is_available():
        logger.warning("Tier A requested but sandbox-exec unavailable, falling back to Tier C")
        return SandboxTier.C

    if requested == SandboxTier.B and not microvm.is_available():
        logger.warning("Tier B requested but MicroVM unavailable, falling back to Tier A")
        if seatbelt.is_available():
            return SandboxTier.A
        logger.warning("Tier A also unavailable, falling back to Tier C")
        return SandboxTier.C

    return requested


def _execute_sandbox_sync(sandbox_id: str, req: CreateSandboxRequest, tier: SandboxTier) -> None:
    """Execute sandbox synchronously (runs in thread for async mode)."""
    state = _sandboxes[sandbox_id]
    state.status = SandboxStatus.RUNNING
    _monitor.register_sandbox(sandbox_id, tier.value)

    # Track VM memory for Tier B
    if tier == SandboxTier.B:
        if not _monitor.allocate_vm_memory(sandbox_id, req.memory_gb):
            state.status = SandboxStatus.FAILED
            state.error = "VM memory budget exceeded"
            _monitor.unregister_sandbox(sandbox_id, success=False)
            return

    # Register per-sandbox allowed domains with proxy
    if req.allowed_domains and _proxy:
        _proxy.add_sandbox_domains(sandbox_id, req.allowed_domains)

    sb_config = _config.get("sandbox", {}).get("seatbelt", {})

    try:
        if tier == SandboxTier.A:
            workspace, exit_code, stdout, stderr, violations = seatbelt.run(
                command=req.command,
                timeout=req.timeout,
                allow_network=req.allow_network,
                denied_paths=sb_config.get("denied_paths"),
                allowed_read_paths=sb_config.get("allowed_read_paths"),
                extra_env=req.env or None,
                files=req.files or None,
                max_cpu_seconds=sb_config.get("max_cpu_seconds", 120),
                max_file_size_mb=sb_config.get("max_file_size_mb", 100),
                max_processes=sb_config.get("max_processes", 50),
            )
            state.workspace = workspace
            state.exit_code = exit_code
            state.stdout = stdout
            state.stderr = stderr
            state.violations = violations

            for v in violations:
                _monitor.record_violation(sandbox_id, v)

        elif tier == SandboxTier.B:
            # MicroVM execution
            try:
                workspace, exit_code, stdout, stderr = microvm.run(
                    command=req.command,
                    timeout=req.timeout,
                    image=req.image,
                    memory_gb=req.memory_gb,
                    cpus=req.cpus,
                    allow_network=req.allow_network,
                    files=req.files or None,
                )
                state.workspace = workspace
                state.exit_code = exit_code
                state.stdout = stdout
                state.stderr = stderr
            except (RuntimeError, NotImplementedError) as e:
                # Graceful degradation to Tier A
                logger.warning("Tier B failed (%s), falling back to Tier A", e)
                workspace, exit_code, stdout, stderr, violations = seatbelt.run(
                    command=req.command,
                    timeout=req.timeout,
                    allow_network=req.allow_network,
                    denied_paths=sb_config.get("denied_paths"),
                    allowed_read_paths=sb_config.get("allowed_read_paths"),
                    extra_env=req.env or None,
                    files=req.files or None,
                )
                state.workspace = workspace
                state.exit_code = exit_code
                state.stdout = stdout
                state.stderr = stderr
                state.violations = violations
                state.tier = SandboxTier.A  # Record actual tier used

        elif tier == SandboxTier.C:
            workspace, exit_code, stdout, stderr = native.run(
                command=req.command,
                timeout=req.timeout,
                extra_env=req.env or None,
                files=req.files or None,
                max_cpu_seconds=sb_config.get("max_cpu_seconds", 120),
                max_file_size_mb=sb_config.get("max_file_size_mb", 100),
                max_processes=sb_config.get("max_processes", 50),
            )
            state.workspace = workspace
            state.exit_code = exit_code
            state.stdout = stdout
            state.stderr = stderr

        # Set final status
        if state.exit_code == 0:
            state.status = SandboxStatus.COMPLETED
        elif state.exit_code == -1 and ("timed out" in state.stderr or "timeout" in state.stderr):
            state.status = SandboxStatus.TIMEOUT
        else:
            state.status = SandboxStatus.FAILED

        _monitor.unregister_sandbox(sandbox_id, success=(state.status == SandboxStatus.COMPLETED))

    except Exception as e:
        state.status = SandboxStatus.FAILED
        state.error = str(e)
        _monitor.unregister_sandbox(sandbox_id, success=False)
        logger.exception("Sandbox %s execution failed", sandbox_id)

    finally:
        # Release VM memory allocation
        if tier == SandboxTier.B:
            _monitor.release_vm_memory(sandbox_id)

        # Remove per-sandbox proxy domains
        if _proxy:
            _proxy.remove_sandbox_domains(sandbox_id)

        # Clean up workspace after collecting results
        if state.workspace and state.workspace.exists():
            seatbelt.destroy_workspace(state.workspace)


@app.post("/sandbox", response_model=SandboxResult, dependencies=[Depends(verify_auth)])
async def create_sandbox(req: CreateSandboxRequest):
    """Create and execute a sandbox."""
    sandbox_id = uuid.uuid4().hex[:12]
    tier = _resolve_tier(req.tier, needs_linux=req.needs_linux)

    # Memory budget check for Tier B
    if tier == SandboxTier.B:
        if not _monitor.can_allocate_vm(req.memory_gb):
            logger.warning(
                "VM memory budget would be exceeded, falling back to Tier A for %s",
                sandbox_id,
            )
            tier = SandboxTier.A if seatbelt.is_available() else SandboxTier.C

    # Create state
    state = SandboxState(
        sandbox_id=sandbox_id,
        tier=tier,
        command=req.command,
        workspace=Path("/tmp"),  # Will be set by executor
    )
    _sandboxes[sandbox_id] = state

    # Async mode: launch in background thread, return immediately
    if req.run_async:
        event = threading.Event()
        _sandbox_events[sandbox_id] = event

        def _run_and_signal():
            try:
                _execute_sandbox_sync(sandbox_id, req, tier)
            finally:
                event.set()
                _background_threads.pop(sandbox_id, None)

        thread = threading.Thread(target=_run_and_signal, daemon=True)
        _background_threads[sandbox_id] = thread
        thread.start()
        # Return immediately — client polls GET /sandbox/{id} for results
        return state.to_result()

    # Synchronous mode: run in thread pool to avoid blocking event loop
    await asyncio.to_thread(_execute_sandbox_sync, sandbox_id, req, tier)
    return state.to_result()


@app.get("/sandbox/{sandbox_id}", response_model=SandboxResult)
async def get_sandbox(sandbox_id: str):
    """Get sandbox status and results."""
    state = _sandboxes.get(sandbox_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    return state.to_result()


@app.post("/sandbox/{sandbox_id}/wait", response_model=SandboxResult)
async def wait_sandbox(sandbox_id: str):
    """Wait for an async sandbox to complete, then return results."""
    state = _sandboxes.get(sandbox_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    # If already done, return immediately
    if state.status not in (SandboxStatus.PENDING, SandboxStatus.RUNNING):
        return state.to_result()

    # Wait for completion event (in thread to avoid blocking event loop)
    event = _sandbox_events.get(sandbox_id)
    if event:
        await asyncio.to_thread(event.wait, 300)  # 5 min max wait
        _sandbox_events.pop(sandbox_id, None)

    return state.to_result()


@app.delete("/sandbox/{sandbox_id}", dependencies=[Depends(verify_auth)])
async def kill_sandbox(sandbox_id: str):
    """Kill a running sandbox and clean up."""
    state = _sandboxes.get(sandbox_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    if state.status == SandboxStatus.RUNNING:
        # Kill the process if we have a PID
        if state.pid:
            try:
                os.kill(state.pid, 9)
            except ProcessLookupError:
                pass
        state.status = SandboxStatus.KILLED

    # Signal completion event so any waiters unblock
    event = _sandbox_events.pop(sandbox_id, None)
    if event:
        event.set()

    # Clean up workspace
    if state.workspace and state.workspace.exists():
        seatbelt.destroy_workspace(state.workspace)

    _monitor.unregister_sandbox(sandbox_id, success=False)
    return {"status": "killed", "id": sandbox_id}


@app.get("/sandboxes", response_model=list[SandboxInfo])
async def list_sandboxes():
    """List all sandboxes (active and recent)."""
    return [state.to_info() for state in _sandboxes.values()]


@app.websocket("/sandbox/{sandbox_id}/stream")
async def stream_sandbox(websocket: WebSocket, sandbox_id: str):
    """WebSocket endpoint for streaming sandbox status updates.

    Sends JSON messages as the sandbox progresses:
      {"type": "status", "status": "running", "id": "..."}
      {"type": "result", "data": {...}}  (final result)
    """
    state = _sandboxes.get(sandbox_id)
    if not state:
        await websocket.close(code=4004, reason="Sandbox not found")
        return

    await websocket.accept()

    try:
        # If already complete, send result and close
        if state.status not in (SandboxStatus.PENDING, SandboxStatus.RUNNING):
            await websocket.send_json({
                "type": "result",
                "data": state.to_result().model_dump(),
            })
            await websocket.close()
            return

        # Send initial status
        await websocket.send_json({
            "type": "status",
            "id": sandbox_id,
            "status": state.status.value,
            "tier": state.tier.value,
        })

        # Poll until completion using the threading event
        event = _sandbox_events.get(sandbox_id)
        if event:
            while not event.is_set():
                # Check in short intervals, send heartbeat
                done = await asyncio.to_thread(event.wait, 2.0)
                if not done:
                    await websocket.send_json({
                        "type": "status",
                        "id": sandbox_id,
                        "status": state.status.value,
                        "elapsed_seconds": round(time.time() - state.started_at, 1),
                    })
        else:
            # No event — synchronous sandbox, poll state
            for _ in range(600):  # 10 min max
                if state.status not in (SandboxStatus.PENDING, SandboxStatus.RUNNING):
                    break
                await asyncio.sleep(1.0)

        # Send final result
        await websocket.send_json({
            "type": "result",
            "data": state.to_result().model_dump(),
        })

    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected from sandbox %s", sandbox_id)
    except Exception as e:
        logger.warning("WebSocket error for sandbox %s: %s", sandbox_id, e)
        try:
            await websocket.close(code=1011, reason=str(e)[:120])
        except Exception:
            pass


@app.get("/metrics")
async def get_metrics():
    """Get sandbox system metrics."""
    metrics = _monitor.get_metrics()
    return {
        "active_sandboxes": metrics.active_sandboxes,
        "active_by_tier": metrics.active_by_tier,
        "vm_memory_allocated_gb": metrics.vm_memory_allocated_gb,
        "vm_memory_cap_gb": metrics.vm_memory_cap_gb,
        "total_violations": metrics.total_violations,
        "total_tasks_completed": metrics.total_tasks_completed,
        "total_tasks_failed": metrics.total_tasks_failed,
    }


@app.get("/health", response_model=HealthResponse)
async def health():
    """Service health check."""
    metrics = _monitor.get_metrics()
    return HealthResponse(
        status="ok",
        version="0.4.0",
        sandbox_exec_available=seatbelt.is_available(),
        virtualization_available=microvm.is_available(),
        active_sandboxes=metrics.active_sandboxes,
        vm_memory_allocated_gb=metrics.vm_memory_allocated_gb,
        proxy_running=_proxy is not None and _proxy._server is not None,
    )


# === SESSION ENDPOINTS (Phase 8C: Persistent Sandbox Sessions) ===


@app.post("/session", response_model=SessionInfo, dependencies=[Depends(verify_auth)])
async def create_session(req: CreateSessionRequest):
    """Create a persistent sandbox session with a long-lived workspace."""
    active_count = sum(1 for s in _sessions.values() if s.active)
    if active_count >= MAX_CONCURRENT_SESSIONS:
        raise HTTPException(status_code=429, detail=f"Max {MAX_CONCURRENT_SESSIONS} concurrent sessions")

    session_id = uuid.uuid4().hex[:12]
    tier = _resolve_tier(req.tier, needs_linux=req.needs_linux)

    # Create workspace
    if tier == SandboxTier.A:
        workspace = seatbelt.create_workspace()
        profile = seatbelt.generate_profile(
            workspace=workspace,
            allow_network=req.allow_network,
        )
        profile_path = seatbelt.write_profile(profile, workspace)
        env = seatbelt.build_env(workspace, req.env or None)
    elif tier == SandboxTier.C:
        workspace = native.create_workspace()
        profile_path = None
        env = native.build_env(workspace, req.env or None)
    else:
        raise HTTPException(status_code=400, detail="Sessions only support Tier A and C currently")

    # Write initial files
    if req.files:
        for filename, content in req.files.items():
            filepath = workspace / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content)

    # Register per-session allowed domains
    if req.allowed_domains and _proxy:
        _proxy.add_sandbox_domains(session_id, req.allowed_domains)

    session = SessionState(
        session_id=session_id,
        tier=tier,
        workspace=workspace,
        profile_path=profile_path,
        env=env,
        ttl_seconds=req.ttl_seconds,
    )
    _sessions[session_id] = session

    logger.info("Created session %s (tier=%s, ttl=%ds)", session_id, tier.value, req.ttl_seconds)
    return session.to_info()


@app.post("/session/{session_id}/exec", response_model=SessionExecResult, dependencies=[Depends(verify_auth)])
async def session_exec(session_id: str, req: SessionExecRequest):
    """Execute a command in an existing session."""
    session = _sessions.get(session_id)
    if not session or not session.active:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found or destroyed")

    if session.is_expired():
        _destroy_session(session_id)
        raise HTTPException(status_code=410, detail="Session expired")

    session.touch()
    session.exec_count += 1
    start_time = time.time()

    sb_config = _config.get("sandbox", {}).get("seatbelt", {})

    if session.paused:
        raise HTTPException(status_code=409, detail="Session is paused — resume before executing")

    if session.tier == SandboxTier.A:
        exit_code, stdout, stderr = await asyncio.to_thread(
            _tracked_seatbelt_execute,
            session=session,
            command=req.command,
            timeout=req.timeout,
            max_cpu_seconds=sb_config.get("max_cpu_seconds", 120),
            max_file_size_mb=sb_config.get("max_file_size_mb", 100),
        )
        violations = []
        for line in stderr.splitlines():
            if "deny" in line.lower() and ("sandbox" in line.lower() or "seatbelt" in line.lower()):
                violations.append(line.strip())
    elif session.tier == SandboxTier.C:
        exit_code, stdout, stderr = await asyncio.to_thread(
            _tracked_native_execute,
            session=session,
            command=req.command,
            timeout=req.timeout,
            max_cpu_seconds=sb_config.get("max_cpu_seconds", 120),
            max_file_size_mb=sb_config.get("max_file_size_mb", 100),
        )
        violations = []
    else:
        raise HTTPException(status_code=400, detail="Unsupported tier for session exec")

    elapsed = time.time() - start_time
    return SessionExecResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        elapsed_seconds=round(elapsed, 3),
        violations=violations,
    )


def _tracked_seatbelt_execute(
    session: SessionState,
    command: str,
    timeout: int = 30,
    max_cpu_seconds: int = 120,
    max_file_size_mb: int = 100,
) -> tuple[int, str, str]:
    """Execute in seatbelt sandbox with PID tracking for pause/resume."""
    cmd_parts = [
        seatbelt.SANDBOX_EXEC,
        "-f", str(session.profile_path),
        "/usr/bin/env", "-i",
    ]
    for key, value in session.env.items():
        cmd_parts.append(f"{key}={value}")
    cmd_parts.extend(["/bin/bash", "-c", command])

    try:
        proc = subprocess.Popen(
            cmd_parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(session.workspace),
            preexec_fn=seatbelt._make_preexec_fn(max_cpu_seconds, max_file_size_mb),
        )
        session._running_pids.append(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stdout, stderr
        finally:
            if proc.pid in session._running_pids:
                session._running_pids.remove(proc.pid)
    except subprocess.TimeoutExpired:
        if proc and proc.pid:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.pid in session._running_pids:
                session._running_pids.remove(proc.pid)
        return -1, "", f"Session exec timed out after {timeout}s"
    except Exception as e:
        return -1, "", f"Session exec error: {e}"


def _tracked_native_execute(
    session: SessionState,
    command: str,
    timeout: int = 30,
    max_cpu_seconds: int = 120,
    max_file_size_mb: int = 100,
) -> tuple[int, str, str]:
    """Execute in native sandbox with PID tracking for pause/resume."""
    full_command = ["/usr/bin/env", "-i"]
    for key, value in session.env.items():
        full_command.append(f"{key}={value}")
    full_command.extend(["/bin/bash", "-c", command])

    try:
        proc = subprocess.Popen(
            full_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(session.workspace),
            preexec_fn=native._make_preexec_fn(max_cpu_seconds, max_file_size_mb),
        )
        session._running_pids.append(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stdout, stderr
        finally:
            if proc.pid in session._running_pids:
                session._running_pids.remove(proc.pid)
    except subprocess.TimeoutExpired:
        if proc and proc.pid:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.pid in session._running_pids:
                session._running_pids.remove(proc.pid)
        return -1, "", f"Session exec timed out after {timeout}s"
    except Exception as e:
        return -1, "", f"Session exec error: {e}"


def _native_execute_in_workspace(
    command: str,
    workspace: Path,
    env: dict[str, str],
    timeout: int = 30,
    max_cpu_seconds: int = 120,
    max_file_size_mb: int = 100,
) -> tuple[int, str, str]:
    """Execute a command in an existing native workspace (no new workspace creation)."""
    full_command = ["/usr/bin/env", "-i"]
    for key, value in env.items():
        full_command.append(f"{key}={value}")
    full_command.extend(["/bin/bash", "-c", command])

    try:
        proc = subprocess.Popen(
            full_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(workspace),
            preexec_fn=native._make_preexec_fn(max_cpu_seconds, max_file_size_mb),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        if proc and proc.pid:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        return -1, "", f"Session exec timed out after {timeout}s"
    except Exception as e:
        return -1, "", f"Session exec error: {e}"


@app.post("/session/{session_id}/files", dependencies=[Depends(verify_auth)])
async def session_write_files(session_id: str, req: SessionWriteRequest):
    """Write files to a session workspace."""
    session = _sessions.get(session_id)
    if not session or not session.active:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found or destroyed")

    session.touch()
    written = []
    for filename, content in req.files.items():
        # Prevent path traversal
        safe_path = Path(filename)
        if safe_path.is_absolute() or ".." in safe_path.parts:
            raise HTTPException(status_code=400, detail=f"Invalid filename: {filename}")
        filepath = session.workspace / safe_path
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content)
        written.append(filename)

    return {"written": written}


@app.get("/session/{session_id}/files/{file_path:path}")
async def session_read_file(session_id: str, file_path: str):
    """Read a file from a session workspace."""
    session = _sessions.get(session_id)
    if not session or not session.active:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found or destroyed")

    session.touch()

    # Prevent path traversal
    safe_path = Path(file_path)
    if safe_path.is_absolute() or ".." in safe_path.parts:
        raise HTTPException(status_code=400, detail=f"Invalid path: {file_path}")

    filepath = session.workspace / safe_path
    if not filepath.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    return PlainTextResponse(filepath.read_text())


@app.get("/session/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str):
    """Get session info."""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return session.to_info()


@app.delete("/session/{session_id}", dependencies=[Depends(verify_auth)])
async def destroy_session(session_id: str):
    """Destroy a persistent session and clean up its workspace."""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    _destroy_session(session_id)
    return {"status": "destroyed", "id": session_id}


@app.post("/session/{session_id}/pause", dependencies=[Depends(verify_auth)])
async def pause_session(session_id: str):
    """Pause a session by sending SIGSTOP to all tracked processes.

    Paused sessions don't count toward idle timeout but still count toward TTL.
    Resume with POST /session/{id}/resume.
    """
    session = _sessions.get(session_id)
    if not session or not session.active:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found or destroyed")
    if session.paused:
        return {"status": "already_paused", "id": session_id}

    # Send SIGSTOP to all tracked PIDs
    stopped = []
    for pid in session._running_pids:
        try:
            os.killpg(pid, signal.SIGSTOP)
            stopped.append(pid)
        except (ProcessLookupError, PermissionError):
            pass  # Process already finished

    session.paused = True
    session.touch()
    logger.info("Paused session %s (%d processes stopped)", session_id, len(stopped))
    return {"status": "paused", "id": session_id, "stopped_pids": len(stopped)}


@app.post("/session/{session_id}/resume", dependencies=[Depends(verify_auth)])
async def resume_session(session_id: str):
    """Resume a paused session by sending SIGCONT to all tracked processes."""
    session = _sessions.get(session_id)
    if not session or not session.active:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found or destroyed")
    if not session.paused:
        return {"status": "already_running", "id": session_id}

    # Send SIGCONT to all tracked PIDs
    resumed = []
    for pid in session._running_pids:
        try:
            os.killpg(pid, signal.SIGCONT)
            resumed.append(pid)
        except (ProcessLookupError, PermissionError):
            pass

    session.paused = False
    session.touch()
    logger.info("Resumed session %s (%d processes resumed)", session_id, len(resumed))
    return {"status": "resumed", "id": session_id, "resumed_pids": len(resumed)}


@app.get("/sessions", response_model=list[SessionInfo])
async def list_sessions():
    """List all sessions (active and recent)."""
    return [s.to_info() for s in _sessions.values()]


@app.post("/session/{session_id}/exec/stream", dependencies=[Depends(verify_auth)])
async def session_exec_stream(session_id: str, req: SessionExecRequest):
    """Execute a command in a session with SSE streaming of stdout/stderr.

    Returns Server-Sent Events:
        event: stdout  data: <line>
        event: stderr  data: <line>
        event: exit    data: {"exit_code": 0, "elapsed_seconds": 1.23}
        event: error   data: {"message": "..."}
    """
    session = _sessions.get(session_id)
    if not session or not session.active:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found or destroyed")

    if session.is_expired():
        _destroy_session(session_id)
        raise HTTPException(status_code=410, detail="Session expired")

    session.touch()
    session.exec_count += 1

    async def _stream_generator():
        import json as _json
        start_time = time.time()

        if session.tier == SandboxTier.A:
            cmd_parts = [
                seatbelt.SANDBOX_EXEC,
                "-f", str(session.profile_path),
                "/usr/bin/env", "-i",
            ]
            for key, value in session.env.items():
                cmd_parts.append(f"{key}={value}")
            cmd_parts.extend(["/bin/bash", "-c", req.command])
        elif session.tier == SandboxTier.C:
            cmd_parts = ["/usr/bin/env", "-i"]
            for key, value in session.env.items():
                cmd_parts.append(f"{key}={value}")
            cmd_parts.extend(["/bin/bash", "-c", req.command])
        else:
            yield {"event": "error", "data": _json.dumps({"message": "Unsupported tier for streaming"})}
            return

        sb_config = _config.get("sandbox", {}).get("seatbelt", {})
        max_cpu = sb_config.get("max_cpu_seconds", 120)
        max_file = sb_config.get("max_file_size_mb", 100)

        try:
            proc = subprocess.Popen(
                cmd_parts,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(session.workspace),
                preexec_fn=seatbelt._make_preexec_fn(max_cpu, max_file),
            )

            import selectors
            sel = selectors.DefaultSelector()
            sel.register(proc.stdout, selectors.EVENT_READ)
            sel.register(proc.stderr, selectors.EVENT_READ)

            open_streams = 2
            while open_streams > 0:
                # Check timeout
                elapsed = time.time() - start_time
                if elapsed > req.timeout:
                    try:
                        os.killpg(proc.pid, 9)
                    except (ProcessLookupError, PermissionError):
                        pass
                    yield {"event": "error", "data": _json.dumps({"message": f"Timed out after {req.timeout}s"})}
                    return

                ready = sel.select(timeout=0.5)
                if not ready:
                    # No data yet, check if process ended
                    if proc.poll() is not None:
                        # Drain remaining output
                        for key, _ in sel.get_map().items():
                            fileobj = sel.get_key(key).fileobj
                            remaining = fileobj.read()
                            if remaining:
                                event_type = "stdout" if fileobj is proc.stdout else "stderr"
                                for line in remaining.splitlines():
                                    yield {"event": event_type, "data": line}
                        break
                    continue

                for key, _ in ready:
                    line = key.fileobj.readline()
                    if not line:
                        sel.unregister(key.fileobj)
                        open_streams -= 1
                        continue
                    event_type = "stdout" if key.fileobj is proc.stdout else "stderr"
                    yield {"event": event_type, "data": line.rstrip("\n")}

            sel.close()

            # Wait for process to finish
            exit_code = proc.wait(timeout=5)
            elapsed = time.time() - start_time

            yield {
                "event": "exit",
                "data": _json.dumps({
                    "exit_code": exit_code,
                    "elapsed_seconds": round(elapsed, 3),
                }),
            }

        except Exception as e:
            yield {"event": "error", "data": _json.dumps({"message": str(e)})}

    return EventSourceResponse(_stream_generator())


def main():
    """Entry point for running the engine server."""
    import uvicorn

    config = load_config()
    host = config.get("engine", {}).get("host", "127.0.0.1")
    port = config.get("engine", {}).get("port", 8093)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
