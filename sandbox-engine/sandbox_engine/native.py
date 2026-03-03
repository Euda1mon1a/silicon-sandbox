"""Tier C: Native process sandbox — subprocess with environment scrubbing and resource limits.

Weakest isolation tier. For trusted internal tools only (linting, formatting, git on validated repos).
No Seatbelt, no VM. Process group isolation + rlimit caps + environment scrubbing.
"""

from __future__ import annotations

import logging
import os
import resource
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def create_workspace() -> Path:
    """Create an ephemeral workspace directory."""
    workspace = Path(tempfile.mkdtemp(prefix="sandbox-native-"))
    logger.info("Created native workspace: %s", workspace)
    return workspace


def destroy_workspace(workspace: Path) -> None:
    """Nuke workspace directory."""
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
        logger.info("Destroyed native workspace: %s", workspace)


def build_env(workspace: Path, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Scrubbed environment — only essential vars."""
    env = {
        "HOME": str(workspace),
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/opt/homebrew/sbin",
        "TMPDIR": str(workspace),
        "LANG": "en_US.UTF-8",
        "TERM": "dumb",
    }
    if extra_env:
        for key, value in extra_env.items():
            key_upper = key.upper()
            if any(s in key_upper for s in ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")):
                logger.warning("Blocked secret-looking env var: %s", key)
                continue
            env[key] = value
    return env


def _make_preexec_fn(
    max_cpu_seconds: int = 120,
    max_file_size_mb: int = 100,
) -> callable:
    """Build a preexec_fn for process group isolation and resource limits.

    Runs in child after fork(), before exec(). More robust than shell ulimit strings.
    """
    def _preexec():
        os.setpgrp()
        resource.setrlimit(resource.RLIMIT_CPU, (max_cpu_seconds, max_cpu_seconds))
        max_bytes = max_file_size_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))
    return _preexec


def run(
    command: str,
    timeout: int = 30,
    extra_env: dict[str, str] | None = None,
    files: dict[str, str] | None = None,
    max_cpu_seconds: int = 120,
    max_file_size_mb: int = 100,
    max_processes: int = 50,
) -> tuple[Path, int, str, str]:
    """Execute a command in a native process sandbox.

    Uses process group isolation (os.setpgrp) and resource.setrlimit
    for robust resource capping without shell ulimit strings.

    Returns (workspace, exit_code, stdout, stderr).
    The caller is responsible for calling destroy_workspace().
    """
    workspace = create_workspace()

    try:
        # Write provided files
        if files:
            for filename, content in files.items():
                filepath = workspace / filename
                filepath.parent.mkdir(parents=True, exist_ok=True)
                filepath.write_text(content)

        env = build_env(workspace, extra_env)

        full_command = [
            "/usr/bin/env", "-i",
        ]
        for key, value in env.items():
            full_command.append(f"{key}={value}")
        full_command.extend([
            "/bin/bash", "-c",
            command,
        ])

        logger.info("Executing in native sandbox: %s", command[:100])

        proc = subprocess.Popen(
            full_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(workspace),
            preexec_fn=_make_preexec_fn(max_cpu_seconds, max_file_size_mb),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return workspace, proc.returncode, stdout, stderr

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
        return workspace, -1, "", f"Native sandbox timed out after {timeout}s"
    except Exception as e:
        destroy_workspace(workspace)
        raise
