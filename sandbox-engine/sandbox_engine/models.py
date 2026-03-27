"""Pydantic models for the sandbox engine."""

from __future__ import annotations

import enum
import time
import uuid
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class SandboxTier(str, enum.Enum):
    A = "A"  # Seatbelt (macOS sandbox-exec)
    B = "B"  # MicroVM (Virtualization.framework)
    C = "C"  # Native process (subprocess + ulimit)
    AUTO = "auto"  # Engine selects based on task requirements


class SandboxStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    TIMEOUT = "timeout"


class CreateSandboxRequest(BaseModel):
    tier: SandboxTier = SandboxTier.A
    command: str
    timeout: int = Field(default=30, ge=1, le=600)
    allow_network: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    image: str = "base"  # Tier B checkpoint name
    memory_gb: int = Field(default=2, ge=1, le=8)  # Tier B only
    cpus: int = Field(default=2, ge=1, le=8)  # Tier B only
    files: dict[str, str] = Field(default_factory=dict)  # filename -> content to inject
    run_async: bool = Field(default=False, alias="async")  # Return immediately, poll for results
    needs_linux: bool = False  # Hint for AUTO tier selection


class SandboxResult(BaseModel):
    id: str
    tier: SandboxTier
    status: SandboxStatus
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    elapsed_seconds: float = 0.0
    workspace: Optional[str] = None
    violations: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class SandboxInfo(BaseModel):
    id: str
    tier: SandboxTier
    status: SandboxStatus
    pid: Optional[int] = None
    command: str
    started_at: float
    elapsed_seconds: float = 0.0
    workspace: Optional[str] = None


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.4.0"
    sandbox_exec_available: bool = False
    virtualization_available: bool = False
    active_sandboxes: int = 0
    vm_memory_allocated_gb: float = 0.0
    proxy_running: bool = False


class CreateSessionRequest(BaseModel):
    tier: SandboxTier = SandboxTier.A
    allow_network: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)  # 1 min to 24 hours
    needs_linux: bool = False
    # Tier B (MicroVM) options
    image: str = "base"  # "base" or "desktop"
    memory_gb: int = Field(default=2, ge=1, le=8)
    cpus: int = Field(default=2, ge=1, le=8)


class SessionExecRequest(BaseModel):
    command: str
    timeout: int = Field(default=30, ge=1, le=600)


class SessionExecResult(BaseModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    elapsed_seconds: float = 0.0
    violations: list[str] = Field(default_factory=list)


class SessionWriteRequest(BaseModel):
    files: dict[str, str]  # filename -> content


class DesktopScreenshotRequest(BaseModel):
    format: str = "png"
    region: Optional[str] = None


class DesktopInputRequest(BaseModel):
    action: str  # click, type, key, scroll, mousemove
    x: Optional[int] = None
    y: Optional[int] = None
    button: int = 1
    text: Optional[str] = None
    combo: Optional[str] = None
    dx: Optional[int] = None
    dy: Optional[int] = None


class DesktopBrowserOpenRequest(BaseModel):
    url: str = "about:blank"


class DesktopBrowserControlRequest(BaseModel):
    cdp_method: str
    cdp_params: Optional[dict] = None


class SessionInfo(BaseModel):
    id: str
    tier: SandboxTier
    status: str  # "active", "paused", or "destroyed"
    created_at: float
    last_activity: float
    ttl_seconds: int
    exec_count: int
    workspace: Optional[str] = None
    paused: bool = False


class SandboxState:
    """Internal mutable state for a running sandbox."""

    def __init__(
        self,
        sandbox_id: str,
        tier: SandboxTier,
        command: str,
        workspace: Path,
    ):
        self.id = sandbox_id
        self.tier = tier
        self.command = command
        self.workspace = workspace
        self.status = SandboxStatus.PENDING
        self.pid: Optional[int] = None
        self.exit_code: Optional[int] = None
        self.stdout = ""
        self.stderr = ""
        self.started_at = time.time()
        self.violations: list[str] = []
        self.error: Optional[str] = None

    def to_result(self) -> SandboxResult:
        return SandboxResult(
            id=self.id,
            tier=self.tier,
            status=self.status,
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            elapsed_seconds=time.time() - self.started_at,
            workspace=str(self.workspace),
            violations=self.violations,
            error=self.error,
        )

    def to_info(self) -> SandboxInfo:
        return SandboxInfo(
            id=self.id,
            tier=self.tier,
            status=self.status,
            pid=self.pid,
            command=self.command,
            started_at=self.started_at,
            elapsed_seconds=time.time() - self.started_at,
            workspace=str(self.workspace),
        )


class SessionState:
    """Internal mutable state for a persistent sandbox session."""

    def __init__(
        self,
        session_id: str,
        tier: SandboxTier,
        workspace: Path,
        profile_path: Optional[Path],
        env: dict[str, str],
        ttl_seconds: int = 3600,
        vm: object | None = None,  # MicroVM instance for Tier B
        image: str = "base",
    ):
        self.id = session_id
        self.tier = tier
        self.workspace = workspace
        self.profile_path = profile_path
        self.env = env
        self.ttl_seconds = ttl_seconds
        self.created_at = time.time()
        self.last_activity = time.time()
        self.exec_count = 0
        self.active = True
        self.paused = False
        self.image = image
        self.vm = vm  # MicroVM instance (Tier B only)
        self._running_pids: list[int] = []  # PIDs of currently running processes

    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = time.time()

    def is_expired(self) -> bool:
        """Check if session has exceeded its TTL."""
        return time.time() - self.created_at > self.ttl_seconds

    def is_idle(self, idle_timeout: float = 900.0) -> bool:
        """Check if session has been idle too long (default 15 min)."""
        return time.time() - self.last_activity > idle_timeout

    def to_info(self) -> SessionInfo:
        if not self.active:
            status = "destroyed"
        elif self.paused:
            status = "paused"
        else:
            status = "active"
        return SessionInfo(
            id=self.id,
            tier=self.tier,
            status=status,
            created_at=self.created_at,
            last_activity=self.last_activity,
            ttl_seconds=self.ttl_seconds,
            exec_count=self.exec_count,
            workspace=str(self.workspace),
            paused=self.paused,
        )
