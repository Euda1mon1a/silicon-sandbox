"""Resource monitoring and violation detection for sandboxes."""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SandboxMetrics:
    """Current sandbox system metrics."""

    active_sandboxes: int = 0
    active_by_tier: dict[str, int] = field(default_factory=dict)
    vm_memory_allocated_gb: float = 0.0
    vm_memory_cap_gb: float = 10.0
    total_violations: int = 0
    total_tasks_completed: int = 0
    total_tasks_failed: int = 0


class SandboxMonitor:
    """Tracks sandbox resource usage and enforces limits."""

    def __init__(self, vm_memory_cap_gb: float = 10.0):
        self.vm_memory_cap_gb = vm_memory_cap_gb
        self._vm_allocations: dict[str, float] = {}  # sandbox_id -> memory_gb
        self._violation_count = 0
        self._tasks_completed = 0
        self._tasks_failed = 0
        self._active_sandboxes: dict[str, str] = {}  # sandbox_id -> tier

    def register_sandbox(self, sandbox_id: str, tier: str) -> None:
        """Register a new active sandbox."""
        self._active_sandboxes[sandbox_id] = tier

    def unregister_sandbox(self, sandbox_id: str, success: bool = True) -> None:
        """Unregister a completed/failed sandbox."""
        self._active_sandboxes.pop(sandbox_id, None)
        self._vm_allocations.pop(sandbox_id, None)
        if success:
            self._tasks_completed += 1
        else:
            self._tasks_failed += 1

    def allocate_vm_memory(self, sandbox_id: str, memory_gb: float) -> bool:
        """Try to allocate VM memory. Returns False if budget exceeded."""
        current_total = sum(self._vm_allocations.values())
        if current_total + memory_gb > self.vm_memory_cap_gb:
            logger.warning(
                "VM memory budget exceeded: %.1f GB allocated + %.1f GB requested > %.1f GB cap",
                current_total,
                memory_gb,
                self.vm_memory_cap_gb,
            )
            return False
        self._vm_allocations[sandbox_id] = memory_gb
        logger.info(
            "Allocated %.1f GB for VM %s (total: %.1f / %.1f GB)",
            memory_gb,
            sandbox_id,
            current_total + memory_gb,
            self.vm_memory_cap_gb,
        )
        return True

    def release_vm_memory(self, sandbox_id: str) -> None:
        """Release VM memory allocation."""
        released = self._vm_allocations.pop(sandbox_id, 0.0)
        if released > 0:
            logger.info("Released %.1f GB from VM %s", released, sandbox_id)

    def record_violation(self, sandbox_id: str, violation: str) -> None:
        """Record a sandbox violation event."""
        self._violation_count += 1
        logger.warning("Sandbox violation [%s]: %s", sandbox_id, violation)

    def get_metrics(self) -> SandboxMetrics:
        """Get current metrics snapshot."""
        tier_counts: dict[str, int] = {}
        for tier in self._active_sandboxes.values():
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

        return SandboxMetrics(
            active_sandboxes=len(self._active_sandboxes),
            active_by_tier=tier_counts,
            vm_memory_allocated_gb=sum(self._vm_allocations.values()),
            vm_memory_cap_gb=self.vm_memory_cap_gb,
            total_violations=self._violation_count,
            total_tasks_completed=self._tasks_completed,
            total_tasks_failed=self._tasks_failed,
        )

    def can_allocate_vm(self, memory_gb: float) -> bool:
        """Check if a VM allocation would succeed without actually allocating."""
        current_total = sum(self._vm_allocations.values())
        return current_total + memory_gb <= self.vm_memory_cap_gb
