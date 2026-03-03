"""Persistent memory store — SQLite + sqlite-vec for vector similarity search."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import sqlite_vec

logger = logging.getLogger(__name__)

EMBEDDING_ENDPOINT = "http://127.0.0.1:8082/v1/embeddings"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


class MemoryStore:
    """SQLite-backed memory with vector similarity search."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database with tables and vector index."""
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)

        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT DEFAULT '',
                source TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at REAL NOT NULL,
                expires_at REAL
            );

            CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                goal TEXT NOT NULL,
                result TEXT DEFAULT '',
                status TEXT DEFAULT '',
                subtask_count INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                elapsed_seconds REAL DEFAULT 0
            );
        """)

        # Create virtual table for vector search
        try:
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0("
                f"  id INTEGER PRIMARY KEY,"
                f"  embedding float[{EMBEDDING_DIM}]"
                f")"
            )
        except sqlite3.OperationalError:
            # Table already exists
            pass

        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    async def add(
        self,
        content: str,
        category: str = "",
        source: str = "",
        metadata: dict[str, Any] | None = None,
        expires_in_hours: float | None = None,
    ) -> int:
        """Add a memory entry with embedding."""
        now = time.time()
        expires_at = now + (expires_in_hours * 3600) if expires_in_hours else None

        cursor = self._conn.execute(
            "INSERT INTO memories (content, category, source, metadata, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (content, category, source, json.dumps(metadata or {}), now, expires_at),
        )
        memory_id = cursor.lastrowid
        self._conn.commit()

        # Generate and store embedding
        try:
            embedding = await self._get_embedding(content)
            self._conn.execute(
                "INSERT INTO memory_vec (id, embedding) VALUES (?, ?)",
                (memory_id, _serialize_vec(embedding)),
            )
            self._conn.commit()
        except Exception as e:
            logger.warning("Failed to generate embedding for memory %d: %s", memory_id, e)

        return memory_id

    async def search(
        self,
        query: str,
        limit: int = 5,
        category: str | None = None,
    ) -> list[dict]:
        """Search memories by vector similarity."""
        try:
            query_embedding = await self._get_embedding(query)
        except Exception as e:
            logger.warning("Embedding generation failed, falling back to text search: %s", e)
            return self._text_search(query, limit, category)

        # Vector similarity search
        rows = self._conn.execute(
            """
            SELECT m.id, m.content, m.category, m.source, m.created_at,
                   vec.distance
            FROM memory_vec vec
            JOIN memories m ON m.id = vec.id
            WHERE vec.embedding MATCH ?
              AND k = ?
            ORDER BY vec.distance
            """,
            (_serialize_vec(query_embedding), limit * 2),
        ).fetchall()

        # Filter by category if specified, and exclude expired
        now = time.time()
        results = []
        for row in rows:
            mem_id, content, cat, source, created_at, distance = row
            if category and cat != category:
                continue
            # Check expiry
            expiry = self._conn.execute(
                "SELECT expires_at FROM memories WHERE id = ?", (mem_id,)
            ).fetchone()
            if expiry and expiry[0] and expiry[0] < now:
                continue
            results.append({
                "content": content,
                "similarity": 1.0 - distance,  # Convert distance to similarity
                "category": cat,
                "source": source,
                "created_at": created_at,
            })
            if len(results) >= limit:
                break

        return results

    def _text_search(
        self, query: str, limit: int, category: str | None,
    ) -> list[dict]:
        """Fallback text search when embeddings unavailable."""
        sql = "SELECT content, category, source, created_at FROM memories WHERE content LIKE ?"
        params: list = [f"%{query}%"]
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "content": r[0],
                "similarity": 0.5,  # Arbitrary score for text match
                "category": r[1],
                "source": r[2],
                "created_at": r[3],
            }
            for r in rows
        ]

    def log_task(
        self,
        task_id: str,
        goal: str,
        result: str = "",
        status: str = "",
        subtask_count: int = 0,
        elapsed_seconds: float = 0,
    ) -> None:
        """Log a completed task to history."""
        self._conn.execute(
            "INSERT INTO task_history (task_id, goal, result, status, subtask_count, created_at, elapsed_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, goal, result, status, subtask_count, time.time(), elapsed_seconds),
        )
        self._conn.commit()

    def get_recent_tasks(self, limit: int = 10) -> list[dict]:
        """Get recent task history."""
        rows = self._conn.execute(
            "SELECT task_id, goal, result, status, subtask_count, created_at, elapsed_seconds "
            "FROM task_history ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "task_id": r[0],
                "goal": r[1],
                "result": r[2][:200],
                "status": r[3],
                "subtask_count": r[4],
                "created_at": r[5],
                "elapsed_seconds": r[6],
            }
            for r in rows
        ]

    def stats(self) -> dict:
        """Get memory store statistics."""
        mem_count = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        task_count = self._conn.execute("SELECT COUNT(*) FROM task_history").fetchone()[0]
        vec_count = self._conn.execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0]
        return {
            "memories": mem_count,
            "vectors": vec_count,
            "tasks": task_count,
            "db_path": str(self.db_path),
        }

    def expire_old(self) -> int:
        """Remove expired memories. Returns count removed."""
        now = time.time()
        # Get IDs to remove from vector table too
        expired = self._conn.execute(
            "SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        ).fetchall()

        if expired:
            ids = [r[0] for r in expired]
            placeholders = ",".join("?" * len(ids))
            self._conn.execute(f"DELETE FROM memory_vec WHERE id IN ({placeholders})", ids)
            self._conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
            self._conn.commit()

        return len(expired)

    async def _get_embedding(self, text: str) -> list[float]:
        """Get embedding vector from the MiniLM service."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                EMBEDDING_ENDPOINT,
                json={
                    "model": EMBEDDING_MODEL,
                    "input": text,
                },
            )
        if resp.status_code != 200:
            raise RuntimeError(f"Embedding API error: {resp.status_code}")

        data = resp.json()
        return data["data"][0]["embedding"]


def _serialize_vec(vec: list[float]) -> bytes:
    """Serialize a float vector to bytes for sqlite-vec."""
    import struct
    return struct.pack(f"{len(vec)}f", *vec)
