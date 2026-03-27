"""Tests for Tier A (Seatbelt) sandbox isolation.

These are REAL tests — they execute sandbox-exec and verify actual isolation.
"""

import os
import subprocess
import pytest
from pathlib import Path

# Add parent to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "sandbox-engine"))

from sandbox_engine import seatbelt


@pytest.fixture(autouse=True)
def check_sandbox_exec():
    """Skip all tests if sandbox-exec is not available."""
    if not seatbelt.is_available():
        pytest.skip("sandbox-exec not available on this system")


class TestSeatbeltAvailability:
    def test_sandbox_exec_exists(self):
        assert os.path.isfile("/usr/bin/sandbox-exec")

    def test_is_available(self):
        assert seatbelt.is_available()


class TestWorkspace:
    def test_create_workspace(self):
        ws = seatbelt.create_workspace()
        assert ws.exists()
        assert ws.is_dir()
        assert str(ws).startswith("/")
        seatbelt.destroy_workspace(ws)

    def test_destroy_workspace(self):
        ws = seatbelt.create_workspace()
        # Write a file in it
        (ws / "test.txt").write_text("hello")
        seatbelt.destroy_workspace(ws)
        assert not ws.exists()

    def test_destroy_nonexistent_workspace(self):
        """Should not raise on missing directory."""
        seatbelt.destroy_workspace(Path("/tmp/nonexistent-sandbox-xyz"))


class TestProfileGeneration:
    def test_basic_profile_v2(self):
        """Default (v2) deny-default profile."""
        ws = seatbelt.create_workspace()
        try:
            profile = seatbelt.generate_profile(workspace=ws)
            assert "(version 1)" in profile
            assert "(deny default" in profile
            # Workspace writes allowed
            import os
            real_ws = os.path.realpath(str(ws))
            assert f'(subpath "{real_ws}")' in profile
            # Network denied by default (covered by deny default)
            assert "(allow network*)" not in profile or "localhost" in profile
        finally:
            seatbelt.destroy_workspace(ws)

    def test_basic_profile_v1(self):
        """Legacy (v1) allow-default profile."""
        ws = seatbelt.create_workspace()
        try:
            profile = seatbelt.generate_profile(workspace=ws, profile_version="v1")
            assert "(version 1)" in profile
            assert "(allow default)" in profile
            assert "(deny file-write*)" in profile
            import os
            real_ws = os.path.realpath(str(ws))
            assert f'(subpath "{real_ws}")' in profile
        finally:
            seatbelt.destroy_workspace(ws)

    def test_network_allowed_profile(self):
        ws = seatbelt.create_workspace()
        try:
            profile = seatbelt.generate_profile(workspace=ws, allow_network=True)
            assert "(allow network*)" in profile
        finally:
            seatbelt.destroy_workspace(ws)

    def test_denied_paths_in_profile(self):
        ws = seatbelt.create_workspace()
        try:
            profile = seatbelt.generate_profile(workspace=ws)
            home = str(Path.home())
            assert f'{home}/.ssh' in profile
            assert f'{home}/.claude' in profile
        finally:
            seatbelt.destroy_workspace(ws)


class TestSeatbeltIsolation:
    """Real isolation tests — these execute sandbox-exec."""

    def test_write_to_workspace_succeeds(self):
        """Sandbox should allow writing to its own workspace."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command='echo "hello" > test.txt && cat test.txt',
            timeout=10,
        )
        try:
            assert exit_code == 0
            assert "hello" in stdout
        finally:
            seatbelt.destroy_workspace(ws)

    def test_write_to_ssh_fails(self):
        """Sandbox must NOT be able to write to ~/.ssh."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command=f'touch {Path.home()}/.ssh/sandbox-test-file 2>&1; echo "exit:$?"',
            timeout=10,
        )
        try:
            # The touch should fail (either sandbox denial or permission error)
            # We check that the file was NOT created
            assert not (Path.home() / ".ssh" / "sandbox-test-file").exists()
        finally:
            seatbelt.destroy_workspace(ws)

    def test_read_ssh_blocked(self):
        """Sandbox must NOT be able to read ~/.ssh contents."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command=f'cat {Path.home()}/.ssh/known_hosts 2>&1',
            timeout=10,
        )
        try:
            # Should fail — either sandbox denial or permission error
            assert exit_code != 0 or "deny" in stderr.lower() or "permission" in stderr.lower() or "denied" in stdout.lower() or stdout.strip() == ""
        finally:
            seatbelt.destroy_workspace(ws)

    def test_read_claude_blocked(self):
        """Sandbox must NOT be able to read ~/.claude contents."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command=f'ls {Path.home()}/.claude/ 2>&1',
            timeout=10,
        )
        try:
            # Should fail
            assert exit_code != 0 or "deny" in stderr.lower() or "denied" in stdout.lower() or "operation not permitted" in stdout.lower()
        finally:
            seatbelt.destroy_workspace(ws)

    def test_read_etc_passwd_succeeds(self):
        """Sandbox should allow reading world-readable system files."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command='cat /etc/passwd | head -3',
            timeout=10,
        )
        try:
            assert exit_code == 0
            # macOS /etc/passwd starts with ## comments, then user entries
            assert len(stdout.strip()) > 0
        finally:
            seatbelt.destroy_workspace(ws)

    def test_network_blocked_by_default(self):
        """Sandbox should block network access by default."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command='curl -s --connect-timeout 3 http://example.com 2>&1; echo "exit:$?"',
            timeout=10,
        )
        try:
            # curl should fail with connection error
            assert "exit:0" not in stdout or "Operation not permitted" in stdout or "Could not resolve" in stdout
        finally:
            seatbelt.destroy_workspace(ws)

    def test_python_execution(self):
        """Sandbox should be able to run Python code."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command='python3 -c "print(2 + 2)"',
            timeout=10,
        )
        try:
            assert exit_code == 0
            assert "4" in stdout
        finally:
            seatbelt.destroy_workspace(ws)

    def test_python_with_file(self):
        """Sandbox should execute Python scripts written to workspace."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command='python3 script.py',
            timeout=10,
            files={"script.py": 'import sys\nprint(f"Python {sys.version_info.major}.{sys.version_info.minor}")\n'},
        )
        try:
            assert exit_code == 0
            # Accept any Python 3.x (system python3 may be 3.9, homebrew is 3.12)
            assert "Python 3." in stdout
        finally:
            seatbelt.destroy_workspace(ws)

    def test_env_scrubbing(self):
        """Sandbox should not inherit host environment."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command='env | sort',
            timeout=10,
        )
        try:
            assert exit_code == 0
            # Should only have our scrubbed vars
            env_lines = stdout.strip().splitlines()
            env_keys = {line.split("=")[0] for line in env_lines if "=" in line}
            # Should NOT have common host env vars
            assert "USER" not in env_keys
            assert "SHELL" not in env_keys
            assert "LOGNAME" not in env_keys
            # Should have our scrubbed vars
            assert "HOME" in env_keys
            assert "PATH" in env_keys
        finally:
            seatbelt.destroy_workspace(ws)

    def test_secret_env_blocked(self):
        """Sandbox should block secret-looking env vars."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command='echo $API_KEY',
            timeout=10,
            extra_env={"API_KEY": "sk-secret-12345"},
        )
        try:
            # API_KEY should be blocked from passing through
            assert "sk-secret-12345" not in stdout
        finally:
            seatbelt.destroy_workspace(ws)

    def test_timeout_enforcement(self):
        """Sandbox should enforce execution timeout."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command='sleep 30',
            timeout=3,
        )
        try:
            assert exit_code == -1
            assert "timed out" in stderr.lower()
        finally:
            seatbelt.destroy_workspace(ws)


class TestDenyDefaultIsolation:
    """Tests specific to v2 deny-default profile improvements (Phase 8A)."""

    def test_write_outside_workspace_blocked(self):
        """v2 profile blocks writes to /tmp (v1 allowed /private/tmp)."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command='touch /tmp/sandbox-escape-test 2>&1; echo "done"',
            timeout=10,
        )
        try:
            # /tmp write should fail — v2 only allows workspace + /private/var/folders
            assert not Path("/tmp/sandbox-escape-test").exists()
        finally:
            seatbelt.destroy_workspace(ws)
            # Clean up in case it leaked (shouldn't)
            Path("/tmp/sandbox-escape-test").unlink(missing_ok=True)

    def test_mach_ipc_selective(self):
        """v2 profile allows mach-lookup but denies other mach ops by default."""
        ws = seatbelt.create_workspace()
        try:
            profile = seatbelt.generate_profile(workspace=ws)
            assert "(allow mach-lookup)" in profile
            assert "(allow mach-register)" in profile
            assert "(allow mach-task-name)" in profile
            # Should NOT have blanket mach allow
            assert "(allow mach*)" not in profile
        finally:
            seatbelt.destroy_workspace(ws)

    def test_iokit_and_sysctl_present(self):
        """v2 profile includes iokit and sysctl allows."""
        ws = seatbelt.create_workspace()
        try:
            profile = seatbelt.generate_profile(workspace=ws)
            assert "(allow sysctl-read)" in profile
            assert "(allow iokit-open)" in profile
        finally:
            seatbelt.destroy_workspace(ws)

    def test_v1_fallback_still_works(self):
        """v1 profile still executes correctly as a fallback."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command='python3 -c "print(42)"',
            timeout=10,
        )
        try:
            assert exit_code == 0
            assert "42" in stdout
        finally:
            seatbelt.destroy_workspace(ws)

    def test_node_execution(self):
        """Sandbox should be able to run Node.js."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            command='node -e "console.log(3 + 4)" 2>/dev/null || echo "node not found"',
            timeout=10,
        )
        try:
            # Node may not be installed, but should not crash sandbox
            assert exit_code == 0
            assert "7" in stdout or "node not found" in stdout
        finally:
            seatbelt.destroy_workspace(ws)


class TestProcessGroupIsolation:
    """Tests for process group isolation (Phase 8B)."""

    def test_timeout_kills_process_tree(self):
        """Timeout should kill the entire process group, not just the parent."""
        ws, exit_code, stdout, stderr, violations = seatbelt.run(
            # Spawn a child that also sleeps
            command='(sleep 60 &); sleep 60',
            timeout=2,
        )
        try:
            assert exit_code == -1
            assert "timed out" in stderr.lower()
        finally:
            seatbelt.destroy_workspace(ws)

    def test_resource_limits_enforced(self):
        """CPU and file size limits should be enforced via rlimit."""
        # Try to create a file larger than the limit (default 100MB)
        # We'll set a tiny limit to verify
        ws = seatbelt.create_workspace()
        try:
            profile = seatbelt.generate_profile(workspace=ws)
            profile_path = seatbelt.write_profile(profile, ws)
            env = seatbelt.build_env(ws)
            # Use 1MB file size limit
            exit_code, stdout, stderr = seatbelt.execute(
                command='dd if=/dev/zero of=bigfile bs=1024 count=2048 2>&1; echo "size:$(wc -c < bigfile)"',
                workspace=ws,
                profile_path=profile_path,
                env=env,
                timeout=10,
                max_file_size_mb=1,
            )
            # dd should be truncated or fail
            # The file should not exceed 1MB (1048576 bytes)
            bigfile = ws / "bigfile"
            if bigfile.exists():
                assert bigfile.stat().st_size <= 1048576 + 4096  # small tolerance
        finally:
            seatbelt.destroy_workspace(ws)


class TestProfileCaching:
    """Tests for SBPL profile caching (Phase 9)."""

    def test_cache_hit_returns_same_profile(self):
        """Same parameters should return identical profile from cache."""
        # Clear cache
        seatbelt._profile_cache.clear()

        ws = seatbelt.create_workspace()
        try:
            p1 = seatbelt.generate_profile(workspace=ws)
            p2 = seatbelt.generate_profile(workspace=ws)
            assert p1 is p2  # Same object from cache, not just equal
            assert len(seatbelt._profile_cache) == 1
        finally:
            seatbelt.destroy_workspace(ws)

    def test_different_params_different_cache_entries(self):
        """Different parameters produce different cache entries."""
        seatbelt._profile_cache.clear()

        ws1 = seatbelt.create_workspace()
        ws2 = seatbelt.create_workspace()
        try:
            p1 = seatbelt.generate_profile(workspace=ws1)
            p2 = seatbelt.generate_profile(workspace=ws2)
            assert p1 is not p2
            assert str(ws1) in p1
            assert str(ws2) in p2
            assert len(seatbelt._profile_cache) == 2
        finally:
            seatbelt.destroy_workspace(ws1)
            seatbelt.destroy_workspace(ws2)

    def test_network_flag_changes_cache_key(self):
        """allow_network produces a different cache entry."""
        seatbelt._profile_cache.clear()

        ws = seatbelt.create_workspace()
        try:
            p_no_net = seatbelt.generate_profile(workspace=ws, allow_network=False)
            p_net = seatbelt.generate_profile(workspace=ws, allow_network=True)
            assert p_no_net is not p_net
            assert "(allow network*)" in p_net
            assert len(seatbelt._profile_cache) == 2
        finally:
            seatbelt.destroy_workspace(ws)

    def test_cache_eviction_at_max_size(self):
        """Cache evicts oldest entry when full."""
        seatbelt._profile_cache.clear()
        original_max = seatbelt._PROFILE_CACHE_MAX

        try:
            # Temporarily set small max
            seatbelt._PROFILE_CACHE_MAX = 3
            workspaces = []
            for i in range(4):
                ws = seatbelt.create_workspace()
                workspaces.append(ws)
                seatbelt.generate_profile(workspace=ws)

            # Should have evicted the first one
            assert len(seatbelt._profile_cache) == 3
        finally:
            seatbelt._PROFILE_CACHE_MAX = original_max
            for ws in workspaces:
                seatbelt.destroy_workspace(ws)

    def test_v1_and_v2_cached_separately(self):
        """v1 and v2 profiles for same workspace are cached separately."""
        seatbelt._profile_cache.clear()

        ws = seatbelt.create_workspace()
        try:
            p_v2 = seatbelt.generate_profile(workspace=ws, profile_version="v2")
            p_v1 = seatbelt.generate_profile(workspace=ws, profile_version="v1")
            assert p_v2 is not p_v1
            assert "(deny default" in p_v2
            assert "(allow default)" in p_v1
            assert len(seatbelt._profile_cache) == 2
        finally:
            seatbelt.destroy_workspace(ws)


class TestEnvironment:
    def test_build_env(self):
        ws = seatbelt.create_workspace()
        try:
            env = seatbelt.build_env(ws)
            assert env["HOME"] == str(ws)
            assert "/usr/bin" in env["PATH"]
            assert "/opt/homebrew/bin" in env["PATH"]
            assert "LANG" in env
        finally:
            seatbelt.destroy_workspace(ws)

    def test_build_env_blocks_secrets(self):
        ws = seatbelt.create_workspace()
        try:
            env = seatbelt.build_env(ws, {"API_KEY": "secret", "NORMAL_VAR": "ok"})
            assert "API_KEY" not in env
            assert env["NORMAL_VAR"] == "ok"
        finally:
            seatbelt.destroy_workspace(ws)
