"""Tests for the Orchestrator components."""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "orchestrator"))
sys.path.insert(0, str(Path(__file__).parent.parent / "sandbox-engine"))

from orchestrator.models import (
    Subtask,
    SubtaskStatus,
    SubtaskType,
    Task,
    TaskStatus,
)
from orchestrator.planner import _fallback_plan, _parse_plan
from orchestrator.memory import MemoryStore


class TestTaskModel:
    def test_create_task(self):
        task = Task(task_id="t1", goal="Write hello world")
        assert task.id == "t1"
        assert task.status == TaskStatus.PENDING

    def test_add_subtasks(self):
        task = Task(task_id="t1", goal="Test")
        st1 = Subtask("s1", SubtaskType.LLM_CALL, "Step 1")
        st2 = Subtask("s2", SubtaskType.CODE_EXEC, "Step 2", depends_on=["s1"])
        task.add_subtask(st1)
        task.add_subtask(st2)
        assert len(task.subtasks) == 2

    def test_get_ready_subtasks(self):
        task = Task(task_id="t1", goal="Test")
        st1 = Subtask("s1", SubtaskType.LLM_CALL, "Step 1")
        st2 = Subtask("s2", SubtaskType.CODE_EXEC, "Step 2", depends_on=["s1"])
        st3 = Subtask("s3", SubtaskType.LLM_CALL, "Step 3")  # No deps
        task.add_subtask(st1)
        task.add_subtask(st2)
        task.add_subtask(st3)

        ready = task.get_ready_subtasks()
        ready_ids = {st.id for st in ready}
        assert "s1" in ready_ids
        assert "s3" in ready_ids
        assert "s2" not in ready_ids  # Blocked by s1

    def test_ready_after_dependency_complete(self):
        task = Task(task_id="t1", goal="Test")
        st1 = Subtask("s1", SubtaskType.LLM_CALL, "Step 1")
        st2 = Subtask("s2", SubtaskType.CODE_EXEC, "Step 2", depends_on=["s1"])
        task.add_subtask(st1)
        task.add_subtask(st2)

        st1.status = SubtaskStatus.COMPLETED
        ready = task.get_ready_subtasks()
        assert len(ready) == 1
        assert ready[0].id == "s2"

    def test_is_complete(self):
        task = Task(task_id="t1", goal="Test")
        st1 = Subtask("s1", SubtaskType.LLM_CALL, "Step 1")
        task.add_subtask(st1)

        assert not task.is_complete()
        st1.status = SubtaskStatus.COMPLETED
        assert task.is_complete()

    def test_has_failures(self):
        task = Task(task_id="t1", goal="Test")
        st1 = Subtask("s1", SubtaskType.LLM_CALL, "Step 1")
        st2 = Subtask("s2", SubtaskType.LLM_CALL, "Step 2")
        task.add_subtask(st1)
        task.add_subtask(st2)

        st1.status = SubtaskStatus.COMPLETED
        st2.status = SubtaskStatus.FAILED
        assert task.has_failures()
        assert task.is_complete()

    def test_to_response(self):
        task = Task(task_id="t1", goal="Hello")
        resp = task.to_response()
        assert resp.id == "t1"
        assert resp.goal == "Hello"
        assert resp.status == TaskStatus.PENDING


class TestPlanParsing:
    def test_parse_valid_json(self):
        response = json.dumps([
            {
                "id": "s1",
                "type": "code_exec",
                "description": "Run hello world",
                "model": "coder",
                "sandbox_tier": "A",
                "depends_on": [],
                "command": "echo hello",
            },
            {
                "id": "s2",
                "type": "synthesis",
                "description": "Summarize",
                "model": "fast",
                "sandbox_tier": "none",
                "depends_on": ["s1"],
                "prompt": "Combine results",
            },
        ])

        subtasks = _parse_plan(response, "t1")
        assert len(subtasks) == 2
        assert subtasks[0].id == "s1"
        assert subtasks[0].type == SubtaskType.CODE_EXEC
        assert subtasks[1].depends_on == ["s1"]

    def test_parse_json_in_code_block(self):
        response = '```json\n[{"id": "s1", "type": "llm_call", "description": "test"}]\n```'
        subtasks = _parse_plan(response, "t1")
        assert len(subtasks) == 1
        assert subtasks[0].id == "s1"

    def test_parse_invalid_json(self):
        subtasks = _parse_plan("this is not json", "t1")
        assert len(subtasks) == 0

    def test_parse_non_array_json(self):
        subtasks = _parse_plan('{"not": "an array"}', "t1")
        assert len(subtasks) == 0

    def test_fallback_plan_code(self):
        task = Task(task_id="t1", goal="Write a Python function that adds two numbers")
        subtasks = _fallback_plan(task)
        assert len(subtasks) >= 2
        assert any(st.type == SubtaskType.SYNTHESIS for st in subtasks)

    def test_fallback_plan_research(self):
        task = Task(task_id="t1", goal="What is the capital of France?")
        subtasks = _fallback_plan(task)
        assert len(subtasks) >= 1
        assert subtasks[0].model == "researcher"


class TestMemoryStore:
    @pytest.fixture
    def memory(self, tmp_path):
        db = MemoryStore(tmp_path / "test_memory.db")
        yield db
        db.close()

    def test_stats_empty(self, memory):
        stats = memory.stats()
        assert stats["memories"] == 0
        assert stats["tasks"] == 0

    def test_log_task(self, memory):
        memory.log_task(
            task_id="t1",
            goal="Test task",
            result="Done",
            status="completed",
            subtask_count=2,
            elapsed_seconds=1.5,
        )
        stats = memory.stats()
        assert stats["tasks"] == 1

    def test_get_recent_tasks(self, memory):
        memory.log_task("t1", "Task 1", "Result 1", "completed")
        memory.log_task("t2", "Task 2", "Result 2", "failed")
        tasks = memory.get_recent_tasks(limit=10)
        assert len(tasks) == 2
        # Most recent first
        assert tasks[0]["task_id"] == "t2"

    def test_expire_old(self, memory):
        # Add a memory that expires in the past
        now = time.time()
        memory._conn.execute(
            "INSERT INTO memories (content, category, created_at, expires_at) VALUES (?, ?, ?, ?)",
            ("old memory", "test", now - 100, now - 10),
        )
        memory._conn.commit()

        removed = memory.expire_old()
        assert removed == 1

    def test_text_search(self, memory):
        memory._conn.execute(
            "INSERT INTO memories (content, category, source, created_at) VALUES (?, ?, ?, ?)",
            ("Python is great for scripting", "fact", "test", time.time()),
        )
        memory._conn.execute(
            "INSERT INTO memories (content, category, source, created_at) VALUES (?, ?, ?, ?)",
            ("JavaScript runs in browsers", "fact", "test", time.time()),
        )
        memory._conn.commit()

        results = memory._text_search("Python", limit=5, category=None)
        assert len(results) == 1
        assert "Python" in results[0]["content"]


class TestOrchestratorServer:
    @pytest.fixture
    def client(self):
        from orchestrator.server import app
        with TestClient(app) as c:
            yield c

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["version"] == "0.2.0"

    def test_list_tasks_empty(self, client):
        r = client.get("/tasks")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_get_task_not_found(self, client):
        r = client.get("/tasks/nonexistent")
        assert r.status_code == 404

    def test_memory_stats(self, client):
        r = client.get("/memory/stats")
        assert r.status_code == 200
        data = r.json()
        assert "memories" in data

    def test_history(self, client):
        r = client.get("/history")
        assert r.status_code == 200
        data = r.json()
        assert "tasks" in data
