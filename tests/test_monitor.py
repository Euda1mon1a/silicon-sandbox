"""Tests for the sandbox resource monitor."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "sandbox-engine"))

from sandbox_engine.monitor import SandboxMonitor


class TestMemoryBudget:
    """Test VM memory budget enforcement."""

    def test_allocate_within_budget(self):
        monitor = SandboxMonitor(vm_memory_cap_gb=10.0)
        assert monitor.allocate_vm_memory("vm1", 2.0) is True
        assert monitor.allocate_vm_memory("vm2", 3.0) is True
        metrics = monitor.get_metrics()
        assert metrics.vm_memory_allocated_gb == 5.0

    def test_allocate_exceeds_budget(self):
        monitor = SandboxMonitor(vm_memory_cap_gb=4.0)
        assert monitor.allocate_vm_memory("vm1", 3.0) is True
        assert monitor.allocate_vm_memory("vm2", 2.0) is False  # Would exceed 4.0
        metrics = monitor.get_metrics()
        assert metrics.vm_memory_allocated_gb == 3.0  # Only first allocation counted

    def test_release_frees_budget(self):
        monitor = SandboxMonitor(vm_memory_cap_gb=4.0)
        assert monitor.allocate_vm_memory("vm1", 3.0) is True
        assert monitor.allocate_vm_memory("vm2", 2.0) is False
        monitor.release_vm_memory("vm1")
        assert monitor.allocate_vm_memory("vm2", 2.0) is True

    def test_can_allocate_check(self):
        monitor = SandboxMonitor(vm_memory_cap_gb=5.0)
        assert monitor.can_allocate_vm(3.0) is True
        monitor.allocate_vm_memory("vm1", 3.0)
        assert monitor.can_allocate_vm(3.0) is False
        assert monitor.can_allocate_vm(2.0) is True

    def test_unregister_also_releases_memory(self):
        monitor = SandboxMonitor(vm_memory_cap_gb=10.0)
        monitor.register_sandbox("vm1", "B")
        monitor.allocate_vm_memory("vm1", 4.0)
        monitor.unregister_sandbox("vm1", success=True)
        metrics = monitor.get_metrics()
        assert metrics.vm_memory_allocated_gb == 0.0


class TestMetrics:
    """Test metrics tracking."""

    def test_task_counting(self):
        monitor = SandboxMonitor()
        monitor.register_sandbox("s1", "A")
        monitor.register_sandbox("s2", "B")
        monitor.unregister_sandbox("s1", success=True)
        monitor.unregister_sandbox("s2", success=False)
        metrics = monitor.get_metrics()
        assert metrics.total_tasks_completed == 1
        assert metrics.total_tasks_failed == 1

    def test_active_by_tier(self):
        monitor = SandboxMonitor()
        monitor.register_sandbox("s1", "A")
        monitor.register_sandbox("s2", "B")
        monitor.register_sandbox("s3", "A")
        metrics = monitor.get_metrics()
        assert metrics.active_sandboxes == 3
        assert metrics.active_by_tier == {"A": 2, "B": 1}

    def test_violation_counting(self):
        monitor = SandboxMonitor()
        monitor.record_violation("s1", "network access denied")
        monitor.record_violation("s1", "file access denied")
        metrics = monitor.get_metrics()
        assert metrics.total_violations == 2
