"""SiliconSandbox SDK client — Sandbox and Session classes.

Usage:
    from silicon_sandbox import Sandbox, Session

    # One-shot execution
    result = Sandbox.run("echo hello")
    print(result.stdout)

    # Context manager (auto-cleanup)
    with Sandbox.create("echo hello") as result:
        print(result.stdout)

    # Persistent session
    with Session.create() as session:
        session.exec("echo 'setup' > config.txt")
        result = session.exec("cat config.txt")
        print(result.stdout)
        session.write_files({"data.json": '{"key": "value"}'})
        content = session.read_file("data.json")
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8093"
DEFAULT_TIMEOUT = 60.0


@dataclass
class ExecResult:
    """Result from a sandbox or session execution."""
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    elapsed_seconds: float = 0.0
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class SandboxResult:
    """Full result from a one-shot sandbox execution."""
    id: str
    tier: str
    status: str
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    elapsed_seconds: float = 0.0
    violations: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.exit_code == 0


class Sandbox:
    """One-shot sandboxed command execution."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @staticmethod
    def run(
        command: str,
        tier: str = "A",
        timeout: int = 30,
        allow_network: bool = False,
        files: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        needs_linux: bool = False,
        base_url: str = DEFAULT_BASE_URL,
    ) -> SandboxResult:
        """Execute a command in a sandbox and return the result.

        This is the simplest way to run sandboxed code:
            result = Sandbox.run("python3 -c 'print(42)'")
        """
        payload = {
            "tier": tier,
            "command": command,
            "timeout": timeout,
            "allow_network": allow_network,
            "needs_linux": needs_linux,
        }
        if files:
            payload["files"] = files
        if env:
            payload["env"] = env

        with httpx.Client(base_url=base_url, timeout=DEFAULT_TIMEOUT) as client:
            resp = client.post("/sandbox", json=payload)
            resp.raise_for_status()
            data = resp.json()

        return SandboxResult(
            id=data["id"],
            tier=data["tier"],
            status=data["status"],
            exit_code=data.get("exit_code"),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            elapsed_seconds=data.get("elapsed_seconds", 0.0),
            violations=data.get("violations", []),
            error=data.get("error"),
        )

    @staticmethod
    def create(
        command: str,
        tier: str = "A",
        timeout: int = 30,
        allow_network: bool = False,
        files: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        needs_linux: bool = False,
        base_url: str = DEFAULT_BASE_URL,
    ) -> _SandboxContext:
        """Create a sandbox as a context manager.

        Usage:
            with Sandbox.create("echo hello") as result:
                print(result.stdout)
        """
        return _SandboxContext(
            command=command,
            tier=tier,
            timeout=timeout,
            allow_network=allow_network,
            files=files,
            env=env,
            needs_linux=needs_linux,
            base_url=base_url,
        )

    @staticmethod
    def health(base_url: str = DEFAULT_BASE_URL) -> dict:
        """Check engine health."""
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            resp = client.get("/health")
            resp.raise_for_status()
            return resp.json()


class _SandboxContext:
    """Context manager for Sandbox.create()."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._result: Optional[SandboxResult] = None

    def __enter__(self) -> SandboxResult:
        self._result = Sandbox.run(**self._kwargs)
        return self._result

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass  # One-shot sandboxes clean up automatically


class Session:
    """Persistent sandbox session with a long-lived workspace."""

    def __init__(
        self,
        session_id: str,
        tier: str,
        base_url: str = DEFAULT_BASE_URL,
        _auto_destroy: bool = True,
    ):
        self.id = session_id
        self.tier = tier
        self._base_url = base_url.rstrip("/")
        self._auto_destroy = _auto_destroy

    @staticmethod
    def create(
        tier: str = "A",
        allow_network: bool = False,
        files: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        ttl_seconds: int = 3600,
        needs_linux: bool = False,
        base_url: str = DEFAULT_BASE_URL,
    ) -> _SessionContext:
        """Create a persistent session as a context manager.

        Usage:
            with Session.create() as session:
                session.exec("echo hello")
                result = session.exec("cat file.txt")
        """
        return _SessionContext(
            tier=tier,
            allow_network=allow_network,
            files=files,
            env=env,
            ttl_seconds=ttl_seconds,
            needs_linux=needs_linux,
            base_url=base_url,
        )

    def exec(self, command: str, timeout: int = 30) -> ExecResult:
        """Execute a command in the session."""
        with httpx.Client(base_url=self._base_url, timeout=timeout + 10) as client:
            resp = client.post(
                f"/session/{self.id}/exec",
                json={"command": command, "timeout": timeout},
            )
            resp.raise_for_status()
            data = resp.json()

        return ExecResult(
            exit_code=data["exit_code"],
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            elapsed_seconds=data.get("elapsed_seconds", 0.0),
            violations=data.get("violations", []),
        )

    def write_files(self, files: dict[str, str]) -> list[str]:
        """Write files to the session workspace. Returns list of written filenames."""
        with httpx.Client(base_url=self._base_url, timeout=10.0) as client:
            resp = client.post(
                f"/session/{self.id}/files",
                json={"files": files},
            )
            resp.raise_for_status()
            return resp.json()["written"]

    def read_file(self, path: str) -> str:
        """Read a file from the session workspace."""
        with httpx.Client(base_url=self._base_url, timeout=10.0) as client:
            resp = client.get(f"/session/{self.id}/files/{path}")
            resp.raise_for_status()
            return resp.text

    def info(self) -> dict:
        """Get session info."""
        with httpx.Client(base_url=self._base_url, timeout=5.0) as client:
            resp = client.get(f"/session/{self.id}")
            resp.raise_for_status()
            return resp.json()

    def destroy(self) -> None:
        """Destroy the session and clean up."""
        with httpx.Client(base_url=self._base_url, timeout=5.0) as client:
            resp = client.delete(f"/session/{self.id}")
            resp.raise_for_status()


class _SessionContext:
    """Context manager for Session.create()."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._session: Optional[Session] = None

    def __enter__(self) -> Session:
        base_url = self._kwargs.pop("base_url", DEFAULT_BASE_URL)
        payload = {
            "tier": self._kwargs.get("tier", "A"),
            "allow_network": self._kwargs.get("allow_network", False),
            "ttl_seconds": self._kwargs.get("ttl_seconds", 3600),
            "needs_linux": self._kwargs.get("needs_linux", False),
        }
        if self._kwargs.get("files"):
            payload["files"] = self._kwargs["files"]
        if self._kwargs.get("env"):
            payload["env"] = self._kwargs["env"]

        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            resp = client.post("/session", json=payload)
            resp.raise_for_status()
            data = resp.json()

        self._session = Session(
            session_id=data["id"],
            tier=data["tier"],
            base_url=base_url,
        )
        return self._session

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            try:
                self._session.destroy()
            except Exception:
                pass  # Best-effort cleanup
