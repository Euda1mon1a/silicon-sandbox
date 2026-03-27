"""Tier A: Seatbelt sandbox using macOS sandbox-exec with dynamic SBPL profiles."""

from __future__ import annotations

import hashlib
import logging
import os
import resource
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Default profile version: "v2" (deny-default) or "v1" (allow-default legacy)
DEFAULT_PROFILE_VERSION = "v2"

# Profile cache: keyed by hash of (workspace, denied_paths, allowed_read_paths, allow_network, version)
# Avoids regenerating identical SBPL profiles across sessions.
_profile_cache: dict[str, str] = {}
_PROFILE_CACHE_MAX = 64


def is_available() -> bool:
    """Check if sandbox-exec is available on this system."""
    return os.path.isfile(SANDBOX_EXEC) and os.access(SANDBOX_EXEC, os.X_OK)


def _resolve_denied_paths(
    denied_paths: list[str] | None = None,
) -> list[str]:
    """Build the full list of always-denied sensitive paths."""
    home = Path.home()
    always_denied = [
        os.path.realpath(str(home / ".ssh")),
        os.path.realpath(str(home / ".gnupg")),
        os.path.realpath(str(home / "Library" / "Keychains")),
        os.path.realpath(str(home / ".claude")),
        os.path.realpath(str(home / ".config" / "git" / "credentials")),
        os.path.realpath(str(home / ".netrc")),
        os.path.realpath(str(home / ".aws")),
    ]
    denied = [os.path.realpath(str(Path(p).expanduser())) for p in (denied_paths or [])]
    return list(dict.fromkeys(always_denied + denied))


def _profile_cache_key(
    workspace: Path,
    denied_paths: list[str] | None,
    allowed_read_paths: list[str] | None,
    allow_network: bool,
    version: str,
) -> str:
    """Compute a cache key for profile parameters.

    The workspace path is normalised so that profiles with equivalent
    workspace paths get the same key (the actual profile text will still
    use the real workspace path — caching only avoids regeneration of
    the SBPL string when the same parameters are reused).
    """
    parts = [
        os.path.realpath(workspace),
        str(sorted(denied_paths or [])),
        str(sorted(allowed_read_paths or [])),
        str(allow_network),
        version,
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def generate_profile(
    workspace: Path,
    denied_paths: list[str] | None = None,
    allowed_read_paths: list[str] | None = None,
    allow_network: bool = False,
    profile_version: str | None = None,
) -> str:
    """Generate a Seatbelt Profile Language (SBPL) profile.

    Results are cached by parameter hash — identical inputs return
    the same profile string without regeneration (up to 64 entries).

    profile_version:
        "v2" (default) — deny-default: everything denied unless explicitly allowed.
            More secure: mach IPC, iokit, sysctl, process ops all denied by default.
        "v1" — allow-default legacy: allow everything, then deny specific operations.
            Less secure but proven stable. Use as fallback if v2 causes issues.
    """
    version = profile_version or DEFAULT_PROFILE_VERSION
    key = _profile_cache_key(workspace, denied_paths, allowed_read_paths, allow_network, version)

    cached = _profile_cache.get(key)
    if cached is not None:
        logger.debug("Profile cache hit: %s", key)
        return cached

    if version == "v1":
        profile = _generate_profile_v1(workspace, denied_paths, allowed_read_paths, allow_network)
    else:
        profile = _generate_profile_v2(workspace, denied_paths, allowed_read_paths, allow_network)

    # Evict oldest entries if cache is full
    if len(_profile_cache) >= _PROFILE_CACHE_MAX:
        oldest = next(iter(_profile_cache))
        del _profile_cache[oldest]

    _profile_cache[key] = profile
    logger.debug("Profile cache miss, stored: %s", key)
    return profile


def _generate_profile_v2(
    workspace: Path,
    denied_paths: list[str] | None = None,
    allowed_read_paths: list[str] | None = None,
    allow_network: bool = False,
) -> str:
    """Generate deny-default SBPL profile (v2).

    Base policy: deny everything. Selectively allow only what's needed:
    - Process exec/fork for running commands
    - Broad file reads (Python/Node need too many paths to enumerate)
    - Explicit denials for sensitive paths (override the broad allow)
    - File writes restricted to workspace + /private/var/folders
    - Mach IPC, sysctl-read, iokit for macOS system calls
    - Network denied by default (optionally allowed)
    """
    workspace = Path(os.path.realpath(workspace))
    all_denied = _resolve_denied_paths(denied_paths)
    allowed_reads = [os.path.realpath(str(Path(p).expanduser())) for p in (allowed_read_paths or [])]

    lines = [
        "(version 1)",
        "",
        ";; Deny-default profile (v2) — fail closed",
        "(deny default (with no-log))",
        "",
        ";; === PROCESS ===",
        "(allow process-exec*)",
        "(allow process-fork)",
        "(allow signal)",
        "",
        ";; === FILE READS ===",
        ";; Broad read access (Python/Node need system libs, homebrew, etc.)",
        "(allow file-read*)",
        "(allow file-read-metadata)",
        "",
        ";; === SENSITIVE PATH DENIALS (override broad read allow) ===",
    ]

    for path in all_denied:
        lines.append(f';; Deny access to {Path(path).name}')
        lines.append(f'(deny file-read* (subpath "{path}"))')
        lines.append(f'(deny file-write* (subpath "{path}"))')
        lines.append("")

    # File writes — workspace and system temp only
    lines.append(";; === FILE WRITES: workspace and temp only ===")
    lines.append(f'(allow file-write* (subpath "{workspace}"))')
    lines.append('(allow file-write* (subpath "/private/var/folders"))')
    lines.append('(allow file-write-data (literal "/dev/null"))')
    lines.append('(allow file-write-data (literal "/dev/tty"))')
    lines.append('(allow file-write-data (literal "/dev/dtracehelper"))')
    lines.append("")

    # Network
    lines.append(";; === NETWORK ===")
    if allow_network:
        lines.append(";; Network allowed (proxy-filtered)")
        lines.append("(allow network*)")
    else:
        lines.append(";; Network denied by default (deny default covers this)")
        lines.append(";; Re-allow localhost for IPC if needed")
        lines.append('(allow network* (remote ip "localhost:*"))')
    lines.append("")

    # Mach IPC — selective (needed for dyld, system calls)
    lines.append(";; === MACH IPC (selective) ===")
    lines.append("(allow mach-lookup)")
    lines.append("(allow mach-register)")
    lines.append("(allow mach-task-name)")
    lines.append("")

    # Sysctl — read only
    lines.append(";; === SYSCTL ===")
    lines.append("(allow sysctl-read)")
    lines.append("")

    # IOKit — needed by some Python modules
    lines.append(";; === IOKIT ===")
    lines.append("(allow iokit-open)")

    return "\n".join(lines)


def _generate_profile_v1(
    workspace: Path,
    denied_paths: list[str] | None = None,
    allowed_read_paths: list[str] | None = None,
    allow_network: bool = False,
) -> str:
    """Generate allow-default SBPL profile (v1 legacy).

    Default policy: allow everything, then deny file-write and network,
    then selectively re-allow workspace writes.
    """
    workspace = Path(os.path.realpath(workspace))
    all_denied = _resolve_denied_paths(denied_paths)

    lines = [
        "(version 1)",
        "",
        ";; Allow-default profile (v1 legacy) — fail open",
        "(allow default)",
        "",
        ";; === FILE WRITE RESTRICTIONS ===",
        "(deny file-write*)",
        "",
        ";; Allow writes to workspace and temp directories",
        f'(allow file-write* (subpath "{workspace}"))',
        f'(allow file-write* (subpath "/private/tmp"))',
        f'(allow file-write* (subpath "/private/var/folders"))',
        f'(allow file-write* (subpath "/var/folders"))',
        "",
        ";; === SENSITIVE PATH DENIALS ===",
    ]

    for path in all_denied:
        lines.append(f';; Deny access to {Path(path).name}')
        lines.append(f'(deny file-read* (subpath "{path}"))')
        lines.append(f'(deny file-write* (subpath "{path}"))')
        lines.append("")

    lines.append(";; === NETWORK ===")
    if not allow_network:
        lines.append("(deny network*)")
        lines.append('(allow network* (remote ip "localhost:*"))')
    else:
        lines.append("(allow network*)")
    lines.append("")

    lines.append(";; === PROCESS ===")
    lines.append("(allow process-exec*)")
    lines.append("(allow process-fork)")
    lines.append("")
    lines.append(";; === SIGNALS ===")
    lines.append("(allow signal (target self))")
    lines.append("")
    lines.append(";; === MACH IPC ===")
    lines.append("(allow mach-lookup)")
    lines.append("")
    lines.append(";; === SYSCTL ===")
    lines.append("(allow sysctl-read)")

    return "\n".join(lines)


def create_workspace() -> Path:
    """Create an ephemeral workspace directory."""
    workspace = Path(tempfile.mkdtemp(prefix="sandbox-"))
    logger.info("Created workspace: %s", workspace)
    return workspace


def destroy_workspace(workspace: Path) -> None:
    """Nuke workspace directory regardless of contents."""
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
        logger.info("Destroyed workspace: %s", workspace)


def write_profile(profile_content: str, workspace: Path) -> Path:
    """Write SBPL profile to a temp file for sandbox-exec."""
    profile_path = workspace / ".sandbox-profile.sb"
    profile_path.write_text(profile_content)
    return profile_path


def build_env(workspace: Path, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a scrubbed environment for the sandboxed process."""
    env = {
        "HOME": str(workspace),
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/opt/homebrew/sbin",
        "TMPDIR": str(workspace),
        "LANG": "en_US.UTF-8",
        "TERM": "dumb",
    }
    if extra_env:
        # Only allow safe env vars (no secrets patterns)
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
    """Build a preexec_fn that isolates the child process.

    Runs in the child process after fork(), before exec():
    1. os.setpgrp() — new process group (enables killpg cleanup)
    2. resource.setrlimit() — CPU time and file size caps
       (more robust than shell ulimit strings which can be circumvented)
    """
    def _preexec():
        # New process group so we can kill the entire tree
        os.setpgrp()
        # CPU time limit (seconds)
        resource.setrlimit(resource.RLIMIT_CPU, (max_cpu_seconds, max_cpu_seconds))
        # File size limit (bytes)
        max_bytes = max_file_size_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))
    return _preexec


def execute(
    command: str,
    workspace: Path,
    profile_path: Path,
    env: dict[str, str],
    timeout: int = 30,
    max_cpu_seconds: int = 120,
    max_file_size_mb: int = 100,
    max_processes: int = 50,
) -> tuple[int, str, str]:
    """Execute a command inside a Seatbelt sandbox.

    Uses process group isolation (os.setpgrp) and resource.setrlimit
    instead of shell ulimit strings for more robust resource capping.

    Returns (exit_code, stdout, stderr).
    """
    full_command = [
        SANDBOX_EXEC,
        "-f", str(profile_path),
        "/usr/bin/env", "-i",
    ]
    # Add env vars
    for key, value in env.items():
        full_command.append(f"{key}={value}")

    full_command.extend([
        "/bin/bash", "-c",
        command,
    ])

    logger.info("Executing in Seatbelt sandbox: %s", command[:100])
    logger.debug("Full command: %s", full_command)

    proc = None
    try:
        proc = subprocess.Popen(
            full_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(workspace),
            preexec_fn=_make_preexec_fn(max_cpu_seconds, max_file_size_mb),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        # Kill the entire process group
        if proc and proc.pid:
            try:
                os.killpg(proc.pid, 9)  # SIGKILL
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        return -1, "", f"Sandbox execution timed out after {timeout}s"
    except Exception as e:
        if proc and proc.pid:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        return -1, "", f"Sandbox execution error: {e}"


def run(
    command: str,
    timeout: int = 30,
    allow_network: bool = False,
    denied_paths: list[str] | None = None,
    allowed_read_paths: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    files: dict[str, str] | None = None,
    max_cpu_seconds: int = 120,
    max_file_size_mb: int = 100,
    max_processes: int = 50,
) -> tuple[Path, int, str, str, list[str]]:
    """High-level: create workspace, generate profile, execute, return results.

    Returns (workspace, exit_code, stdout, stderr, violations).
    The caller is responsible for calling destroy_workspace().
    """
    workspace = create_workspace()

    try:
        # Write any provided files into workspace
        if files:
            for filename, content in files.items():
                filepath = workspace / filename
                filepath.parent.mkdir(parents=True, exist_ok=True)
                filepath.write_text(content)

        profile = generate_profile(
            workspace=workspace,
            denied_paths=denied_paths,
            allowed_read_paths=allowed_read_paths,
            allow_network=allow_network,
        )
        profile_path = write_profile(profile, workspace)
        env = build_env(workspace, extra_env)

        exit_code, stdout, stderr = execute(
            command=command,
            workspace=workspace,
            profile_path=profile_path,
            env=env,
            timeout=timeout,
            max_cpu_seconds=max_cpu_seconds,
            max_file_size_mb=max_file_size_mb,
            max_processes=max_processes,
        )

        # Check for violations in stderr (sandbox denial messages)
        violations = []
        for line in stderr.splitlines():
            if "deny" in line.lower() and ("sandbox" in line.lower() or "seatbelt" in line.lower()):
                violations.append(line.strip())

        return workspace, exit_code, stdout, stderr, violations

    except Exception as e:
        destroy_workspace(workspace)
        raise
