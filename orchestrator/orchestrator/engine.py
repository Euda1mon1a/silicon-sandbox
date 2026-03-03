"""DAG execution engine — runs subtask graphs with parallel dispatch."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

import httpx

from .models import (
    Subtask,
    SubtaskStatus,
    SubtaskType,
    Task,
    TaskStatus,
)
from .router import ModelRouter

logger = logging.getLogger(__name__)

# Sandbox Engine endpoint
SANDBOX_API = "http://127.0.0.1:8093"


class DAGEngine:
    """Executes subtask DAGs with parallel dispatch and dependency resolution."""

    def __init__(
        self,
        router: ModelRouter,
        max_concurrent: int = 3,
        sandbox_api: str = SANDBOX_API,
        on_subtask_event: Callable | None = None,
    ):
        self.router = router
        self.max_concurrent = max_concurrent
        self.sandbox_api = sandbox_api
        self._on_event = on_subtask_event  # callback for WS streaming

    async def execute(self, task: Task) -> None:
        """Execute all subtasks in the DAG, respecting dependencies."""
        task.status = TaskStatus.RUNNING
        semaphore = asyncio.Semaphore(self.max_concurrent)

        try:
            while not task.is_complete():
                ready = task.get_ready_subtasks()
                if not ready:
                    # Check for deadlock (no ready tasks but not complete)
                    running = [
                        st for st in task.subtasks.values()
                        if st.status == SubtaskStatus.RUNNING
                    ]
                    if not running:
                        logger.error("DAG deadlock: no ready or running subtasks")
                        task.status = TaskStatus.FAILED
                        task.error = "DAG execution deadlocked — circular dependency?"
                        return
                    # Wait for running tasks to finish
                    await asyncio.sleep(0.5)
                    continue

                # Launch ready subtasks in parallel (up to concurrency limit)
                coros = []
                for subtask in ready:
                    subtask.status = SubtaskStatus.RUNNING
                    subtask.started_at = time.time()
                    self._emit("subtask_started", task, subtask)
                    coros.append(self._run_subtask(task, subtask, semaphore))

                await asyncio.gather(*coros, return_exceptions=True)

            # All subtasks done — set final status
            if task.has_failures():
                task.status = TaskStatus.FAILED
                failed = [st for st in task.subtasks.values() if st.status == SubtaskStatus.FAILED]
                task.error = f"{len(failed)} subtask(s) failed"
            else:
                task.status = TaskStatus.COMPLETED

            # Collect final result from the last synthesis subtask
            synthesis = [
                st for st in task.subtasks.values()
                if st.type == SubtaskType.SYNTHESIS and st.status == SubtaskStatus.COMPLETED
            ]
            if synthesis:
                task.result = synthesis[-1].output
            else:
                # Use output from last completed subtask
                completed = [
                    st for st in task.subtasks.values()
                    if st.status == SubtaskStatus.COMPLETED
                ]
                if completed:
                    task.result = completed[-1].output

            self._emit("task_completed", task, None)

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            logger.exception("DAG execution failed for task %s", task.id)

    async def _run_subtask(
        self, task: Task, subtask: Subtask, semaphore: asyncio.Semaphore,
    ) -> None:
        """Execute a single subtask."""
        async with semaphore:
            try:
                if subtask.type == SubtaskType.CODE_EXEC:
                    await self._execute_code(task, subtask)
                elif subtask.type == SubtaskType.LLM_CALL:
                    await self._execute_llm_call(task, subtask)
                elif subtask.type == SubtaskType.SYNTHESIS:
                    await self._execute_synthesis(task, subtask)
                elif subtask.type == SubtaskType.WEB_RESEARCH:
                    await self._execute_llm_call(task, subtask)  # LLM-based for now
                elif subtask.type == SubtaskType.FILE_OPERATION:
                    await self._execute_code(task, subtask)
                else:
                    await self._execute_llm_call(task, subtask)

                subtask.status = SubtaskStatus.COMPLETED
                subtask.elapsed_seconds = time.time() - (subtask.started_at or time.time())
                self._emit("subtask_completed", task, subtask)

            except Exception as e:
                subtask.status = SubtaskStatus.FAILED
                subtask.error = str(e)
                subtask.elapsed_seconds = time.time() - (subtask.started_at or time.time())
                logger.warning("Subtask %s failed: %s", subtask.id, e)
                self._emit("subtask_failed", task, subtask)

    async def _execute_code(self, task: Task, subtask: Subtask) -> None:
        """Execute code in a sandbox via the Sandbox Engine API."""
        command = subtask.command

        # If command references a previous subtask's output, resolve it
        if command.startswith("# Will be filled"):
            # Get code from the dependency's output
            for dep_id in subtask.depends_on:
                dep = task.subtasks.get(dep_id)
                if dep and dep.output:
                    command = self._extract_code(dep.output)
                    break

        if not command or command.startswith("#"):
            subtask.output = "(no command to execute)"
            return

        # Determine tier
        tier = subtask.sandbox_tier if subtask.sandbox_tier != "none" else "A"

        # Call sandbox engine
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.sandbox_api}/sandbox",
                json={
                    "tier": tier,
                    "command": command,
                    "timeout": 30,
                },
            )

        if resp.status_code != 200:
            raise RuntimeError(f"Sandbox API error: {resp.status_code}")

        result = resp.json()
        subtask.output = result.get("stdout", "")
        if result.get("stderr"):
            subtask.output += f"\nSTDERR: {result['stderr']}"
        if result.get("exit_code", 0) != 0:
            raise RuntimeError(
                f"Command exited with code {result.get('exit_code')}: "
                f"{result.get('stderr', '')[:200]}"
            )

    async def _execute_llm_call(self, task: Task, subtask: Subtask) -> None:
        """Execute an LLM call via the model router."""
        prompt = subtask.prompt or subtask.description

        # Inject dependency outputs into prompt
        if subtask.depends_on:
            dep_outputs = []
            for dep_id in subtask.depends_on:
                dep = task.subtasks.get(dep_id)
                if dep and dep.output:
                    dep_outputs.append(f"[{dep.id}: {dep.description}]\n{dep.output}")
            if dep_outputs:
                context = "\n\n".join(dep_outputs)
                prompt = f"Previous step outputs:\n{context}\n\nTask: {prompt}"

        role = subtask.model or "coder"
        subtask.output = await self.router.call(
            role=role,
            prompt=prompt,
            max_tokens=4096,
        )

    async def _execute_synthesis(self, task: Task, subtask: Subtask) -> None:
        """Synthesize results from dependency outputs."""
        dep_outputs = []
        for dep_id in subtask.depends_on:
            dep = task.subtasks.get(dep_id)
            if dep and dep.output:
                dep_outputs.append(f"### {dep.description}\n{dep.output}")

        if not dep_outputs:
            subtask.output = "(no outputs to synthesize)"
            return

        combined = "\n\n---\n\n".join(dep_outputs)
        prompt = (
            f"Original goal: {task.goal}\n\n"
            f"Step outputs:\n{combined}\n\n"
            f"Synthesize these into a clear, complete final response."
        )

        role = subtask.model or "fast"
        subtask.output = await self.router.call(
            role=role,
            prompt=prompt,
            system="You are a synthesis agent. Combine the provided outputs into a coherent final answer. Be concise but thorough.",
            max_tokens=4096,
        )

    def _extract_code(self, text: str) -> str:
        """Extract executable code from LLM output."""
        # Look for code blocks
        if "```python" in text:
            code = text.split("```python")[1].split("```")[0].strip()
            return f"python3 -c {self._shell_quote(code)}"
        elif "```bash" in text or "```sh" in text:
            marker = "```bash" if "```bash" in text else "```sh"
            code = text.split(marker)[1].split("```")[0].strip()
            return code
        elif "```" in text:
            code = text.split("```")[1].split("```")[0].strip()
            # Guess language
            if code.startswith("def ") or code.startswith("import ") or "print(" in code:
                return f"python3 -c {self._shell_quote(code)}"
            return code
        # No code block — treat entire text as code
        return text.strip()

    def _shell_quote(self, s: str) -> str:
        """Shell-quote a string for use in command."""
        return "'" + s.replace("'", "'\"'\"'") + "'"

    def _emit(self, event_type: str, task: Task, subtask: Subtask | None) -> None:
        """Emit an event for WebSocket streaming."""
        if self._on_event:
            try:
                self._on_event(event_type, task, subtask)
            except Exception:
                pass
