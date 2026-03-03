"""SiliconSandbox Orchestrator — FastAPI server for task orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from .engine import DAGEngine
from .memory import MemoryStore
from .models import (
    CreateTaskRequest,
    MemorySearchResult,
    Task,
    TaskResponse,
    TaskStatus,
)
from .planner import plan_task
from .router import ModelRouter

logger = logging.getLogger(__name__)

# Global state
_tasks: dict[str, Task] = {}
_router: ModelRouter | None = None
_memory: MemoryStore | None = None
_engine: DAGEngine | None = None
_config: dict = {}
_ws_clients: list[WebSocket] = []
_background_threads: dict[str, threading.Thread] = {}


def load_config() -> dict:
    """Load config from default.yaml."""
    config_paths = [
        Path(__file__).parent.parent.parent / "config" / "default.yaml",
        Path.home() / "workspace" / "silicon-sandbox" / "config" / "default.yaml",
    ]
    for p in config_paths:
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f)
    return {}


def _broadcast_event(event_type: str, task: Task, subtask=None):
    """Queue a WebSocket event for all connected clients."""
    data = {
        "type": event_type,
        "task_id": task.id,
        "timestamp": time.time(),
    }
    if subtask:
        data["subtask"] = subtask.to_response().model_dump()

    if event_type == "task_completed":
        data["result"] = task.result[:500]
        data["status"] = task.status.value

    # Store for async broadcasting (WS clients consume from main event loop)
    _pending_events.append(data)


_pending_events: list[dict] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    global _router, _memory, _engine, _config

    _config = load_config()

    # Initialize model router
    model_config = _config.get("orchestrator", {}).get("models", {})
    _router = ModelRouter(model_config)

    # Initialize memory store
    db_path = _config.get("orchestrator", {}).get("memory_db", "./data/memory.db")
    if not Path(db_path).is_absolute():
        db_path = Path(__file__).parent.parent.parent / db_path
    _memory = MemoryStore(db_path)

    # Initialize DAG engine
    max_concurrent = _config.get("orchestrator", {}).get("max_concurrent_tasks", 3)
    _engine = DAGEngine(
        router=_router,
        max_concurrent=max_concurrent,
        on_subtask_event=_broadcast_event,
    )

    logger.info("SiliconSandbox Orchestrator starting")
    logger.info("  Memory DB: %s", db_path)
    logger.info("  Max concurrent: %d", max_concurrent)
    logger.info("  Models: %s", ", ".join(model_config.keys()))

    yield

    # Shutdown
    if _memory:
        _memory.close()

    # Wait for background threads
    for thread in _background_threads.values():
        if thread.is_alive():
            thread.join(timeout=2.0)
    _background_threads.clear()


app = FastAPI(
    title="SiliconSandbox Orchestrator",
    version="0.2.0",
    lifespan=lifespan,
)


@app.post("/tasks", response_model=TaskResponse)
async def create_task(req: CreateTaskRequest):
    """Submit a goal for task decomposition and execution."""
    task_id = uuid.uuid4().hex[:12]
    task = Task(
        task_id=task_id,
        goal=req.goal,
        context=req.context,
        timeout=req.timeout,
    )
    _tasks[task_id] = task

    # Plan: decompose goal into subtasks
    memory_context = ""
    if _memory:
        try:
            results = await _memory.search(req.goal, limit=3)
            if results:
                memory_context = "\n".join(r["content"] for r in results)
        except Exception as e:
            logger.warning("Memory search failed: %s", e)

    subtasks = await plan_task(task, _router, max_subtasks=req.max_subtasks, memory_context=memory_context)
    for st in subtasks:
        task.add_subtask(st)

    # Execute in background thread (so the API returns immediately)
    done_event = threading.Event()

    def _run_task():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_engine.execute(task))
            # Log to memory
            if _memory:
                _memory.log_task(
                    task_id=task.id,
                    goal=task.goal,
                    result=task.result[:1000],
                    status=task.status.value,
                    subtask_count=len(task.subtasks),
                    elapsed_seconds=time.time() - task.created_at,
                )
                # Store successful results as memory
                if task.status == TaskStatus.COMPLETED and task.result:
                    loop.run_until_complete(_memory.add(
                        content=f"Task: {task.goal}\nResult: {task.result[:500]}",
                        category="task_result",
                        source=f"task:{task.id}",
                    ))
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            logger.exception("Task %s execution failed", task_id)
        finally:
            done_event.set()
            _background_threads.pop(task_id, None)
            loop.close()

    thread = threading.Thread(target=_run_task, daemon=True)
    _background_threads[task_id] = thread
    thread.start()

    return task.to_response()


@app.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Get task status and results."""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task.to_response()


@app.get("/tasks", response_model=list[TaskResponse])
async def list_tasks(limit: int = 20):
    """List recent tasks."""
    tasks = sorted(_tasks.values(), key=lambda t: t.created_at, reverse=True)
    return [t.to_response() for t in tasks[:limit]]


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Cancel a running task."""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if task.status in (TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.RUNNING):
        task.status = TaskStatus.CANCELLED
        return {"status": "cancelled", "id": task_id}

    return {"status": task.status.value, "id": task_id}


@app.get("/memory/search")
async def search_memory(q: str, limit: int = 5, category: str | None = None):
    """Search persistent memory."""
    if not _memory:
        raise HTTPException(status_code=503, detail="Memory store not initialized")

    results = await _memory.search(q, limit=limit, category=category)
    return {"results": results, "count": len(results)}


@app.get("/memory/stats")
async def memory_stats():
    """Get memory store statistics."""
    if not _memory:
        raise HTTPException(status_code=503, detail="Memory store not initialized")
    return _memory.stats()


@app.get("/history")
async def task_history(limit: int = 20):
    """Get task execution history from memory store."""
    if not _memory:
        return {"tasks": [], "count": 0}
    tasks = _memory.get_recent_tasks(limit=limit)
    return {"tasks": tasks, "count": len(tasks)}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time task progress streaming."""
    await websocket.accept()
    _ws_clients.append(websocket)

    try:
        while True:
            # Drain pending events
            while _pending_events:
                event = _pending_events.pop(0)
                for client in list(_ws_clients):
                    try:
                        await client.send_json(event)
                    except Exception:
                        _ws_clients.remove(client)

            # Also accept client messages (for future interactive use)
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                # Client can send ping or subscribe to specific tasks
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                pass

    except WebSocketDisconnect:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)
    except Exception:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


@app.get("/health")
async def health():
    """Service health check."""
    return {
        "status": "ok",
        "version": "0.2.0",
        "active_tasks": sum(
            1 for t in _tasks.values()
            if t.status in (TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.RUNNING)
        ),
        "total_tasks": len(_tasks),
        "memory_available": _memory is not None,
        "models_configured": list((_config.get("orchestrator", {}).get("models", {})).keys()),
    }


def main():
    """Entry point for running the orchestrator server."""
    import uvicorn

    config = load_config()
    host = config.get("orchestrator", {}).get("host", "127.0.0.1")
    port = config.get("orchestrator", {}).get("port", 8094)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
