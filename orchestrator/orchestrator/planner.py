"""Task planner — decomposes natural language goals into subtask DAGs via LLM."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from .models import Subtask, SubtaskType, Task
from .router import ModelRouter

logger = logging.getLogger(__name__)

PLANNER_SYSTEM = """You are a task planner for SiliconSandbox, an AI agent execution platform.

Given a user goal, decompose it into a DAG of subtasks. Each subtask should be atomic and executable.

Respond with a JSON array of subtasks. Each subtask has:
- "id": short unique identifier (e.g., "s1", "s2")
- "type": one of "code_exec", "web_research", "file_operation", "synthesis", "llm_call"
- "description": what this subtask does (1-2 sentences)
- "model": which LLM to use — "coder" for code generation, "researcher" for analysis/research, "fast" for simple tasks, "planner" for complex reasoning
- "sandbox_tier": "A" for macOS code, "B" for Linux/browser, "C" for trusted tools, "none" for LLM-only
- "depends_on": list of subtask IDs this depends on (empty for root tasks)
- "command": shell command to execute (for code_exec/file_operation)
- "prompt": prompt for the LLM (for llm_call/synthesis)

Rules:
1. Minimize subtask count (prefer fewer, well-scoped tasks)
2. Mark dependencies correctly — subtasks can run in parallel if independent
3. End with a "synthesis" subtask that combines results into a final answer
4. For code tasks, write the code in the command field
5. Keep it practical — don't over-decompose simple tasks

Respond ONLY with the JSON array, no other text."""


async def plan_task(
    task: Task,
    router: ModelRouter,
    max_subtasks: int = 10,
    memory_context: str = "",
) -> list[Subtask]:
    """Use the planner LLM to decompose a task goal into subtasks."""
    task.status = task.status.PLANNING

    prompt = f"Goal: {task.goal}"
    if task.context:
        prompt += f"\n\nAdditional context: {task.context}"
    if memory_context:
        prompt += f"\n\nRelevant prior context:\n{memory_context}"
    prompt += f"\n\nMaximum {max_subtasks} subtasks."

    try:
        response = await router.call(
            role="planner",
            prompt=prompt,
            system=PLANNER_SYSTEM,
            max_tokens=4096,
            temperature=0.3,
        )
    except Exception as e:
        logger.error("Planner LLM call failed: %s", e)
        # Fallback: create a single direct execution subtask
        return _fallback_plan(task)

    subtasks = _parse_plan(response, task.id)
    if not subtasks:
        logger.warning("Planner returned no subtasks, using fallback")
        return _fallback_plan(task)

    return subtasks[:max_subtasks]


def _parse_plan(response: str, task_id: str) -> list[Subtask]:
    """Parse LLM response into Subtask objects."""
    # Try to extract JSON from the response
    response = response.strip()

    # Handle markdown code blocks
    if "```json" in response:
        response = response.split("```json")[1].split("```")[0].strip()
    elif "```" in response:
        response = response.split("```")[1].split("```")[0].strip()

    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        logger.warning("Failed to parse planner response as JSON")
        return []

    if not isinstance(data, list):
        logger.warning("Planner response is not a JSON array")
        return []

    subtasks = []
    for item in data:
        try:
            st_type = SubtaskType(item.get("type", "llm_call"))
        except ValueError:
            st_type = SubtaskType.LLM_CALL

        subtask = Subtask(
            subtask_id=item.get("id", uuid.uuid4().hex[:6]),
            task_type=st_type,
            description=item.get("description", ""),
            model=item.get("model", "coder"),
            sandbox_tier=item.get("sandbox_tier", "A"),
            depends_on=item.get("depends_on", []),
            command=item.get("command", ""),
            prompt=item.get("prompt", ""),
        )
        subtasks.append(subtask)

    return subtasks


def _fallback_plan(task: Task) -> list[Subtask]:
    """Create a simple single-step plan when the planner fails."""
    # Detect if this looks like a code task
    code_keywords = ("write", "code", "function", "script", "program", "implement", "create a")
    is_code = any(kw in task.goal.lower() for kw in code_keywords)

    subtasks = []

    if is_code:
        subtasks.append(Subtask(
            subtask_id="s1",
            task_type=SubtaskType.LLM_CALL,
            description=f"Generate code for: {task.goal}",
            model="coder",
            sandbox_tier="none",
            prompt=f"Write code to accomplish this goal: {task.goal}\n\nRespond with only the code.",
        ))
        subtasks.append(Subtask(
            subtask_id="s2",
            task_type=SubtaskType.CODE_EXEC,
            description="Execute the generated code",
            model="",
            sandbox_tier="A",
            depends_on=["s1"],
            command="# Will be filled from s1 output",
        ))
    else:
        subtasks.append(Subtask(
            subtask_id="s1",
            task_type=SubtaskType.LLM_CALL,
            description=f"Answer: {task.goal}",
            model="researcher",
            sandbox_tier="none",
            prompt=task.goal,
        ))

    # Synthesis step
    subtasks.append(Subtask(
        subtask_id="final",
        task_type=SubtaskType.SYNTHESIS,
        description="Synthesize final result",
        model="fast",
        sandbox_tier="none",
        depends_on=[st.id for st in subtasks],
        prompt="Combine the outputs of the previous steps into a clear, complete response.",
    ))

    return subtasks
