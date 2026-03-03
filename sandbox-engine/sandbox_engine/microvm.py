"""Tier B: MicroVM sandbox using Apple Virtualization.framework via vm-launcher CLI.

Boots an Alpine Linux MicroVM, communicates with the guest agent over
the serial console (hvc0), and executes commands in full isolation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Paths — resolved relative to project root (~/workspace/silicon-sandbox/)
# microvm.py lives at sandbox-engine/sandbox_engine/microvm.py
_SANDBOX_ENGINE_DIR = Path(__file__).parent.parent  # sandbox-engine/
_PROJECT_ROOT = _SANDBOX_ENGINE_DIR.parent           # silicon-sandbox/
_VM_LAUNCHER = _SANDBOX_ENGINE_DIR / "vm-launcher" / ".build" / "release" / "vm-launcher"
_KERNEL = _PROJECT_ROOT / "config" / "vm-images" / "Image"
_INITRD = _PROJECT_ROOT / "config" / "vm-images" / "initramfs.cpio.gz"


def is_available() -> bool:
    """Check if MicroVM support is available."""
    if not _VM_LAUNCHER.exists():
        logger.debug("vm-launcher binary not found at %s", _VM_LAUNCHER)
        return False
    if not _KERNEL.exists():
        logger.debug("Kernel Image not found at %s", _KERNEL)
        return False
    if not _INITRD.exists():
        logger.debug("Initramfs not found at %s", _INITRD)
        return False
    return True


class MicroVM:
    """Manages the lifecycle of a single MicroVM instance."""

    def __init__(
        self,
        cpus: int = 2,
        memory_gb: int = 1,
        allow_network: bool = False,
        shared_dirs: list[tuple[str, str]] | None = None,
    ):
        self.cpus = cpus
        self.memory_gb = memory_gb
        self.allow_network = allow_network
        self.shared_dirs = shared_dirs or []  # list of (host_path, guest_tag)
        self._process: subprocess.Popen | None = None
        self._ready = threading.Event()
        self._response_lines: list[str] = []
        self._response_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._req_counter = 0

    def start(self, timeout: float = 15.0) -> None:
        """Boot the MicroVM and wait for the guest agent to be ready."""
        cmd = [
            str(_VM_LAUNCHER),
            "boot",
            "--kernel", str(_KERNEL),
            "--initrd", str(_INITRD),
            "--cpus", str(self.cpus),
            "--memory", str(self.memory_gb),
        ]
        if self.allow_network:
            cmd.append("--allow-net")

        for host_path, tag in self.shared_dirs:
            cmd.extend(["--share", f"{host_path}:{tag}"])

        logger.info("Starting MicroVM: %s", " ".join(cmd))
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Read stdout in a background thread to detect SANDBOX_READY
        self._reader_thread = threading.Thread(
            target=self._read_output, daemon=True
        )
        self._reader_thread.start()

        # Wait for the guest to be ready
        if not self._ready.wait(timeout=timeout):
            self.stop()
            raise TimeoutError(
                f"MicroVM did not become ready within {timeout}s"
            )
        logger.info("MicroVM ready")

    def _read_output(self) -> None:
        """Background thread: read stdout from the VM process."""
        assert self._process and self._process.stdout
        for raw_line in self._process.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")

            if line == "SANDBOX_READY":
                self._ready.set()
                continue

            if line.startswith("RESP:"):
                with self._response_lock:
                    self._response_lines.append(line[5:])
                continue

            # Kernel/boot output — log it
            if line:
                logger.debug("[vm] %s", line)

    def _send_request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        """Send a JSON-RPC request to the guest agent and wait for a response."""
        assert self._process and self._process.stdin

        self._req_counter += 1
        req_id = self._req_counter

        request = {"method": method, "params": params or {}, "id": req_id}
        request_json = json.dumps(request) + "\n"

        # Clear any pending responses
        with self._response_lock:
            self._response_lines.clear()

        # Send request
        self._process.stdin.write(request_json.encode("utf-8"))
        self._process.stdin.flush()

        # Wait for response
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._response_lock:
                if self._response_lines:
                    response_str = self._response_lines.pop(0)
                    try:
                        return json.loads(response_str)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON response: %s", response_str[:200])
                        continue
            time.sleep(0.05)

        raise TimeoutError(f"No response from guest agent within {timeout}s")

    def ping(self) -> dict:
        """Ping the guest agent."""
        return self._send_request("ping")

    def exec_command(self, command: str, timeout: int = 30) -> dict:
        """Execute a command in the guest."""
        response = self._send_request(
            "exec",
            {"command": command, "timeout": timeout},
            timeout=timeout + 5,  # Extra time for protocol overhead
        )

        result = response.get("result", {})

        # Decode base64 output
        if "stdout_b64" in result:
            try:
                result["stdout"] = base64.b64decode(result.pop("stdout_b64")).decode("utf-8", errors="replace")
            except Exception:
                result["stdout"] = ""
        if "stderr_b64" in result:
            try:
                result["stderr"] = base64.b64decode(result.pop("stderr_b64")).decode("utf-8", errors="replace")
            except Exception:
                result["stderr"] = ""

        return result

    def write_file(self, path: str, content: str) -> dict:
        """Write a file in the guest workspace."""
        content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        response = self._send_request(
            "write_file",
            {"path": path, "content_b64": content_b64},
        )
        return response.get("result", response)

    def read_file(self, path: str) -> str:
        """Read a file from the guest workspace."""
        response = self._send_request("read_file", {"path": path})
        result = response.get("result", {})
        if "content_b64" in result:
            return base64.b64decode(result["content_b64"]).decode("utf-8", errors="replace")
        if "error" in response:
            raise FileNotFoundError(response["error"])
        return result.get("content", "")

    def shutdown(self) -> None:
        """Ask the guest to shut down gracefully."""
        try:
            self._send_request("shutdown", timeout=5)
        except TimeoutError:
            pass

    def stop(self) -> None:
        """Stop the VM (forcefully if needed)."""
        if self._process:
            if self._process.poll() is None:
                try:
                    self.shutdown()
                    self._process.wait(timeout=5)
                except (TimeoutError, subprocess.TimeoutExpired):
                    self._process.kill()
                    self._process.wait(timeout=3)
            self._process = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def run(
    command: str,
    timeout: int = 30,
    image: str = "base",
    memory_gb: int = 1,
    cpus: int = 2,
    allow_network: bool = False,
    files: dict[str, str] | None = None,
    shared_dirs: list[tuple[str, str]] | None = None,
) -> tuple[None, int, str, str]:
    """Execute a command in a MicroVM sandbox.

    Returns (workspace, exit_code, stdout, stderr).
    The workspace is always None for MicroVM (ephemeral, in-RAM).
    """
    if not is_available():
        raise RuntimeError(
            "MicroVM sandbox not available. "
            "vm-launcher binary not built or VM images not found."
        )

    with MicroVM(
        cpus=cpus,
        memory_gb=memory_gb,
        allow_network=allow_network,
        shared_dirs=shared_dirs,
    ) as vm:
        # Upload files to workspace
        if files:
            for filename, content in files.items():
                vm.write_file(filename, content)

        # Execute the command
        result = vm.exec_command(command, timeout=timeout)

        return (
            None,
            result.get("exit_code", -1),
            result.get("stdout", ""),
            result.get("stderr", ""),
        )
