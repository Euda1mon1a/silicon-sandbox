"""Tests for the MicroVM (Tier B) sandbox."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "sandbox-engine"))

from sandbox_engine import microvm
from sandbox_engine.microvm import MicroVM


class TestMicroVMAvailability:
    def test_is_available(self):
        assert microvm.is_available() is True

    def test_paths_exist(self):
        assert microvm._VM_LAUNCHER.exists()
        assert microvm._KERNEL.exists()
        assert microvm._INITRD.exists()


class TestMicroVMLifecycle:
    def test_boot_and_ping(self):
        vm = MicroVM(cpus=1, memory_gb=1)
        try:
            vm.start(timeout=15)
            response = vm.ping()
            assert response["result"]["status"] == "ok"
            assert response["result"]["hostname"] == "sandbox"
        finally:
            vm.stop()

    def test_context_manager(self):
        with MicroVM(cpus=1, memory_gb=1) as vm:
            response = vm.ping()
            assert response["result"]["status"] == "ok"


class TestMicroVMExecution:
    def test_echo(self):
        with MicroVM(cpus=1, memory_gb=1) as vm:
            result = vm.exec_command("echo hello", timeout=10)
            assert result["exit_code"] == 0
            assert result["status"] == "completed"
            assert "hello" in result["stdout"]

    def test_exit_code(self):
        with MicroVM(cpus=1, memory_gb=1) as vm:
            result = vm.exec_command("false", timeout=10)
            assert result["exit_code"] != 0

    def test_multiline_output(self):
        with MicroVM(cpus=1, memory_gb=1) as vm:
            result = vm.exec_command("echo line1; echo line2; echo line3", timeout=10)
            assert result["exit_code"] == 0
            lines = result["stdout"].strip().split("\n")
            assert len(lines) == 3

    def test_uname(self):
        with MicroVM(cpus=1, memory_gb=1) as vm:
            result = vm.exec_command("uname -a", timeout=10)
            assert result["exit_code"] == 0
            assert "Linux" in result["stdout"]
            assert "aarch64" in result["stdout"]


class TestMicroVMFiles:
    def test_write_and_read_file(self):
        with MicroVM(cpus=1, memory_gb=1) as vm:
            vm.write_file("test.txt", "hello world")
            content = vm.read_file("test.txt")
            assert "hello world" in content

    def test_exec_with_written_file(self):
        with MicroVM(cpus=1, memory_gb=1) as vm:
            vm.write_file("script.sh", "#!/bin/sh\necho from-script")
            result = vm.exec_command("sh /workspace/script.sh", timeout=10)
            assert result["exit_code"] == 0
            assert "from-script" in result["stdout"]

    def test_read_nonexistent_file(self):
        with MicroVM(cpus=1, memory_gb=1) as vm:
            with pytest.raises(FileNotFoundError):
                vm.read_file("nonexistent.txt")


class TestMicroVMRun:
    """Test the high-level run() function."""

    def test_simple_run(self):
        _, exit_code, stdout, stderr = microvm.run(
            command="echo from-run",
            timeout=10,
            cpus=1,
            memory_gb=1,
        )
        assert exit_code == 0
        assert "from-run" in stdout

    def test_run_with_files(self):
        _, exit_code, stdout, stderr = microvm.run(
            command="cat hello.txt",
            timeout=10,
            cpus=1,
            memory_gb=1,
            files={"hello.txt": "world"},
        )
        assert exit_code == 0
        assert "world" in stdout


class TestMicroVMNetwork:
    """Test NAT networking (Phase 3)."""

    def test_network_disabled_by_default(self):
        """Without allow_network, no eth0 exists."""
        with MicroVM(cpus=1, memory_gb=1) as vm:
            result = vm.exec_command("ip link show eth0 2>&1", timeout=10)
            assert "can't find device" in result["stdout"] or result["exit_code"] != 0

    def test_network_enabled_has_eth0(self):
        """With allow_network, eth0 gets a DHCP address."""
        with MicroVM(cpus=1, memory_gb=1, allow_network=True) as vm:
            result = vm.exec_command("ip addr show eth0", timeout=10)
            assert result["exit_code"] == 0
            assert "inet " in result["stdout"]  # Has IPv4 address
            assert "192.168.64" in result["stdout"]

    def test_network_dns_works(self):
        """DNS resolution works over NAT."""
        with MicroVM(cpus=1, memory_gb=1, allow_network=True) as vm:
            result = vm.exec_command("nslookup pypi.org 2>&1", timeout=10)
            assert "Address:" in result["stdout"]

    def test_network_http_works(self):
        """HTTP requests work over NAT."""
        with MicroVM(cpus=1, memory_gb=1, allow_network=True) as vm:
            result = vm.exec_command(
                "wget -qO- --timeout=5 http://httpbin.org/ip 2>&1",
                timeout=15,
            )
            assert result["exit_code"] == 0
            assert "origin" in result["stdout"]


class TestMicroVMVirtioFS:
    """Test VirtioFS directory sharing (Phase 3)."""

    def test_share_directory_read(self):
        """Host directory is readable inside guest via VirtioFS."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write test files on host
            Path(tmpdir, "hello.txt").write_text("shared content")
            Path(tmpdir, "data.csv").write_text("a,b,c\n1,2,3\n")

            with MicroVM(
                cpus=1,
                memory_gb=1,
                shared_dirs=[(tmpdir, "testshare")],
            ) as vm:
                result = vm.exec_command("cat /mnt/testshare/hello.txt", timeout=10)
                assert result["exit_code"] == 0
                assert "shared content" in result["stdout"]

    def test_share_directory_listing(self):
        """Can list files in shared directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "file1.txt").write_text("one")
            Path(tmpdir, "file2.txt").write_text("two")

            with MicroVM(
                cpus=1,
                memory_gb=1,
                shared_dirs=[(tmpdir, "listing")],
            ) as vm:
                result = vm.exec_command("ls /mnt/listing/", timeout=10)
                assert result["exit_code"] == 0
                assert "file1.txt" in result["stdout"]
                assert "file2.txt" in result["stdout"]

    def test_share_is_read_only(self):
        """VirtioFS shares are mounted read-only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "readonly.txt").write_text("don't modify me")

            with MicroVM(
                cpus=1,
                memory_gb=1,
                shared_dirs=[(tmpdir, "rotest")],
            ) as vm:
                result = vm.exec_command(
                    "echo nope > /mnt/rotest/readonly.txt 2>&1; echo RC=$?",
                    timeout=10,
                )
                assert "RC=1" in result["stdout"] or "Operation not permitted" in result["stdout"]

    def test_share_coexists_with_workspace(self):
        """VirtioFS shares don't interfere with workspace file operations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "shared.txt").write_text("from host")

            with MicroVM(
                cpus=1,
                memory_gb=1,
                shared_dirs=[(tmpdir, "coexist")],
            ) as vm:
                # Read shared file
                result = vm.exec_command("cat /mnt/coexist/shared.txt", timeout=10)
                assert "from host" in result["stdout"]

                # Write to workspace (should still work)
                vm.write_file("local.txt", "from guest")
                content = vm.read_file("local.txt")
                assert "from guest" in content
