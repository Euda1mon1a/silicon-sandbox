"""Pydantic models for the orchestrator."""

from __future__ import annotations

import enum
import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubtaskType(str, enum.Enum):
    CODE_EXEC = "code_exec"
    WEB_RESEARCH = "web_research"
    FILE_OPERATION = "file_operation"
    BROWSER_ACTION = "browser_action"
    SYNTHESIS = "synthesis"
    LLM_CALL = "llm_call"


class SubtaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# --- Request / Response ---

class CreateTaskRequest(BaseModel):
    goal: str
    context: str = ""
    max_subtasks: int = Field(default=10, ge=1, le=50)
    timeout: int = Field(default=600, ge=30, le=3600)


class DirectSubtaskRequest(BaseModel):
    """A pre-built subtask for direct execution (no LLM decomposition)."""
    id: str = ""
    type: SubtaskType = SubtaskType.CODE_EXEC
    description: str
    command: str = ""
    prompt: str = ""
    model: str = ""
    sandbox_tier: str = "C"
    depends_on: list[str] = Field(default_factory=list)
    timeout: int = Field(default=30, ge=1, le=600)


class CreateDirectTaskRequest(BaseModel):
    """Submit a pre-built subtask DAG without LLM decomposition."""
    goal: str
    subtasks: list[DirectSubtaskRequest]
    timeout: int = Field(default=600, ge=30, le=3600)


class SubtaskResult(BaseModel):
    id: str
    type: SubtaskType
    description: str
    status: SubtaskStatus
    model: str = ""
    sandbox_tier: str = ""
    depends_on: list[str] = Field(default_factory=list)
    output: str = ""
    error: Optional[str] = None
    elapsed_seconds: float = 0.0
    started_at: Optional[float] = None


class TaskResponse(BaseModel):
    id: str
    goal: str
    status: TaskStatus
    subtasks: list[SubtaskResult] = Field(default_factory=list)
    result: str = ""
    error: Optional[str] = None
    created_at: float
    elapsed_seconds: float = 0.0


class MemorySearchResult(BaseModel):
    content: str
    similarity: float
    category: str = ""
    source: str = ""
    created_at: Optional[float] = None


# --- Internal state ---

class Subtask:
    """Mutable internal state for a subtask."""

    def __init__(
        self,
        subtask_id: str,
        task_type: SubtaskType,
        description: str,
        model: str = "",
        sandbox_tier: str = "A",
        depends_on: list[str] | None = None,
        command: str = "",
        prompt: str = "",
        inputs: dict[str, Any] | None = None,
        timeout: int = 30,
    ):
        self.id = subtask_id
        self.type = task_type
        self.description = description
        self.model = model
        self.sandbox_tier = sandbox_tier
        self.depends_on = depends_on or []
        self.command = command
        self.prompt = prompt
        self.inputs = inputs or {}
        self.timeout = timeout
        self.status = SubtaskStatus.PENDING
        self.output = ""
        self.error: str | None = None
        self.started_at: float | None = None
        self.elapsed_seconds = 0.0

    def to_response(self) -> SubtaskResult:
        return SubtaskResult(
            id=self.id,
            type=self.type,
            description=self.description,
            status=self.status,
            model=self.model,
            sandbox_tier=self.sandbox_tier,
            depends_on=self.depends_on,
            output=self.output,
            error=self.error,
            elapsed_seconds=self.elapsed_seconds,
            started_at=self.started_at,
        )


class Task:
    """Mutable internal state for a task with its subtask DAG."""

    def __init__(self, task_id: str, goal: str, context: str = "", timeout: int = 600):
        self.id = task_id
        self.goal = goal
        self.context = context
        self.timeout = timeout
        self.status = TaskStatus.PENDING
        self.subtasks: dict[str, Subtask] = {}  # subtask_id -> Subtask
        self.result = ""
        self.error: str | None = None
        self.created_at = time.time()

    def add_subtask(self, subtask: Subtask) -> None:
        self.subtasks[subtask.id] = subtask

    def get_ready_subtasks(self) -> list[Subtask]:
        """Return subtasks whose dependencies are all completed."""
        ready = []
        for st in self.subtasks.values():
            if st.status != SubtaskStatus.PENDING:
                continue
            deps_met = all(
                self.subtasks[dep_id].status == SubtaskStatus.COMPLETED
                for dep_id in st.depends_on
                if dep_id in self.subtasks
            )
            if deps_met:
                ready.append(st)
        return ready

    def is_complete(self) -> bool:
        """All subtasks finished (completed, failed, or skipped)."""
        return all(
            st.status in (SubtaskStatus.COMPLETED, SubtaskStatus.FAILED, SubtaskStatus.SKIPPED)
            for st in self.subtasks.values()
        )

    def has_failures(self) -> bool:
        return any(st.status == SubtaskStatus.FAILED for st in self.subtasks.values())

    def to_response(self) -> TaskResponse:
        elapsed = time.time() - self.created_at
        return TaskResponse(
            id=self.id,
            goal=self.goal,
            status=self.status,
            subtasks=[st.to_response() for st in self.subtasks.values()],
            result=self.result,
            error=self.error,
            created_at=self.created_at,
            elapsed_seconds=elapsed,
        )
