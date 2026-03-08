"""
SQLite database layer for Agent Relay.

All DB operations are async via aiosqlite. WAL mode for concurrent reads.
"""

import json
import secrets
import uuid
from datetime import datetime, timezone

import aiosqlite

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    version TEXT DEFAULT '1.0.0',
    capabilities TEXT NOT NULL DEFAULT '[]',
    contact_method TEXT NOT NULL DEFAULT 'poll',
    webhook_url TEXT,
    trust_tier INTEGER DEFAULT 3,
    permissions TEXT NOT NULL DEFAULT '{}',
    status TEXT DEFAULT 'offline',
    last_seen TEXT,
    metadata TEXT,
    api_key_hash TEXT,
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    from_agent TEXT NOT NULL REFERENCES agents(agent_id),
    to_agent TEXT NOT NULL REFERENCES agents(agent_id),
    status TEXT NOT NULL DEFAULT 'submitted',
    priority TEXT DEFAULT 'normal',
    due_by TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    from_agent TEXT NOT NULL,
    role TEXT DEFAULT 'agent',
    parts TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    name TEXT NOT NULL,
    parts TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    task_id TEXT,
    message_id TEXT,
    target_agent TEXT NOT NULL REFERENCES agents(agent_id),
    method TEXT NOT NULL DEFAULT 'poll',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    last_attempt TEXT,
    acknowledged_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    agent_id TEXT,
    task_id TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_to_agent ON tasks(to_agent, status);
CREATE INDEX IF NOT EXISTS idx_tasks_from_agent ON tasks(from_agent);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_target ON deliveries(target_agent, status);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id(prefix: str = "") -> str:
    short = uuid.uuid4().hex[:12]
    return f"{prefix}{short}" if prefix else short


def _hash_key(key: str) -> str:
    """Simple hash for API key storage. Not bcrypt — keys are high-entropy random tokens."""
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    # --- Agents ---

    async def register_agent(self, agent_id: str, name: str, description: str | None,
                             version: str, capabilities: list, contact_method: str,
                             webhook_url: str | None, trust_tier: int,
                             permissions: dict, metadata: dict | None,
                             api_key: str | None = None) -> tuple[dict, str]:
        """Register or update an agent. Returns (agent_dict, api_key)."""
        now = _now()
        if api_key is None:
            api_key = secrets.token_hex(32)
        key_hash = _hash_key(api_key)

        existing = await self.get_agent(agent_id)
        if existing:
            # Update — but preserve existing api_key unless new one provided
            await self._db.execute("""
                UPDATE agents SET name=?, description=?, version=?, capabilities=?,
                    contact_method=?, webhook_url=?, trust_tier=?, permissions=?,
                    metadata=?, api_key_hash=?, updated_at=?
                WHERE agent_id=?
            """, (name, description, version, json.dumps(capabilities),
                  contact_method, webhook_url, trust_tier, json.dumps(permissions),
                  json.dumps(metadata) if metadata else None, key_hash, now, agent_id))
        else:
            await self._db.execute("""
                INSERT INTO agents (agent_id, name, description, version, capabilities,
                    contact_method, webhook_url, trust_tier, permissions, metadata,
                    api_key_hash, registered_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (agent_id, name, description, version, json.dumps(capabilities),
                  contact_method, webhook_url, trust_tier, json.dumps(permissions),
                  json.dumps(metadata) if metadata else None, key_hash, now, now))

        await self._db.commit()
        await self.audit("agent.registered", agent_id=agent_id)
        agent = await self.get_agent(agent_id)
        return agent, api_key

    async def get_agent(self, agent_id: str) -> dict | None:
        cursor = await self._db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_agent(row)

    async def list_agents(self) -> list[dict]:
        cursor = await self._db.execute("SELECT * FROM agents ORDER BY agent_id")
        rows = await cursor.fetchall()
        return [self._row_to_agent(r) for r in rows]

    async def verify_api_key(self, agent_id: str, api_key: str) -> bool:
        cursor = await self._db.execute(
            "SELECT api_key_hash FROM agents WHERE agent_id=?", (agent_id,))
        row = await cursor.fetchone()
        if not row:
            return False
        return row["api_key_hash"] == _hash_key(api_key)

    async def heartbeat(self, agent_id: str, status: str = "online") -> bool:
        now = _now()
        cursor = await self._db.execute(
            "UPDATE agents SET status=?, last_seen=?, updated_at=? WHERE agent_id=?",
            (status, now, now, agent_id))
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete_agent(self, agent_id: str) -> bool:
        cursor = await self._db.execute("DELETE FROM agents WHERE agent_id=?", (agent_id,))
        await self._db.commit()
        if cursor.rowcount > 0:
            await self.audit("agent.deleted", agent_id=agent_id)
            return True
        return False

    def _row_to_agent(self, row) -> dict:
        return {
            "agent_id": row["agent_id"],
            "name": row["name"],
            "description": row["description"],
            "version": row["version"],
            "capabilities": json.loads(row["capabilities"]),
            "contact": {
                "method": row["contact_method"],
                "webhook_url": row["webhook_url"],
            },
            "trust_tier": row["trust_tier"],
            "permissions": json.loads(row["permissions"]),
            "status": row["status"],
            "last_seen": row["last_seen"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
            "registered_at": row["registered_at"],
            "updated_at": row["updated_at"],
        }

    # --- Tasks ---

    async def create_task(self, from_agent: str, to_agent: str, title: str,
                          description: str | None, priority: str, due_by: str | None,
                          metadata: dict | None) -> dict:
        task_id = _gen_id("task_")
        now = _now()
        await self._db.execute("""
            INSERT INTO tasks (task_id, title, description, from_agent, to_agent,
                status, priority, due_by, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?)
        """, (task_id, title, description, from_agent, to_agent, priority,
              due_by, json.dumps(metadata) if metadata else None, now, now))

        # Create delivery record
        delivery_id = _gen_id("del_")
        agent = await self.get_agent(to_agent)
        method = agent["contact"]["method"] if agent else "poll"
        await self._db.execute("""
            INSERT INTO deliveries (delivery_id, task_id, target_agent, method, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (delivery_id, task_id, to_agent, method, now))

        await self._db.commit()
        await self.audit("task.created", agent_id=from_agent, task_id=task_id,
                         detail={"to": to_agent, "title": title, "priority": priority})
        return await self.get_task(task_id)

    async def get_task(self, task_id: str) -> dict | None:
        cursor = await self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    async def get_task_with_messages(self, task_id: str) -> dict | None:
        task = await self.get_task(task_id)
        if not task:
            return None
        task["messages"] = await self.get_messages(task_id)
        task["artifacts"] = await self.get_artifacts(task_id)
        return task

    async def update_task_status(self, task_id: str, status: str,
                                 agent_id: str | None = None) -> dict | None:
        now = _now()
        await self._db.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
            (status, now, task_id))
        await self._db.commit()
        await self.audit("task.updated", agent_id=agent_id, task_id=task_id,
                         detail={"status": status})
        return await self.get_task(task_id)

    async def list_tasks(self, to_agent: str | None = None, from_agent: str | None = None,
                         status: str | None = None, since: str | None = None,
                         limit: int = 50) -> list[dict]:
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if to_agent:
            query += " AND to_agent=?"
            params.append(to_agent)
        if from_agent:
            query += " AND from_agent=?"
            params.append(from_agent)
        if status:
            query += " AND status=?"
            params.append(status)
        if since:
            query += " AND created_at>=?"
            params.append(since)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self._db.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    def _row_to_task(self, row) -> dict:
        return {
            "task_id": row["task_id"],
            "title": row["title"],
            "description": row["description"],
            "from_agent": row["from_agent"],
            "to_agent": row["to_agent"],
            "status": row["status"],
            "priority": row["priority"],
            "due_by": row["due_by"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # --- Messages ---

    async def create_message(self, task_id: str, from_agent: str, content: str | None,
                             parts: list | None = None, role: str = "agent") -> dict:
        message_id = _gen_id("msg_")
        now = _now()
        if parts is None and content:
            parts = [{"type": "text", "content": content}]
        elif parts is None:
            parts = []
        await self._db.execute("""
            INSERT INTO messages (message_id, task_id, from_agent, role, parts, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (message_id, task_id, from_agent, role, json.dumps(parts), now))

        # Update task updated_at
        await self._db.execute(
            "UPDATE tasks SET updated_at=? WHERE task_id=?", (now, task_id))

        # Create delivery for the other party
        task = await self.get_task(task_id)
        if task:
            target = task["to_agent"] if from_agent == task["from_agent"] else task["from_agent"]
            delivery_id = _gen_id("del_")
            agent = await self.get_agent(target)
            method = agent["contact"]["method"] if agent else "poll"
            await self._db.execute("""
                INSERT INTO deliveries (delivery_id, message_id, task_id, target_agent,
                    method, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """, (delivery_id, message_id, task_id, target, method, now))

        await self._db.commit()
        await self.audit("message.sent", agent_id=from_agent, task_id=task_id,
                         detail={"message_id": message_id})
        return await self._get_message(message_id)

    async def _get_message(self, message_id: str) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM messages WHERE message_id=?", (message_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_message(row)

    async def get_messages(self, task_id: str) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM messages WHERE task_id=? ORDER BY created_at", (task_id,))
        rows = await cursor.fetchall()
        return [self._row_to_message(r) for r in rows]

    def _row_to_message(self, row) -> dict:
        return {
            "message_id": row["message_id"],
            "task_id": row["task_id"],
            "from_agent": row["from_agent"],
            "role": row["role"],
            "parts": json.loads(row["parts"]),
            "created_at": row["created_at"],
        }

    # --- Artifacts ---

    async def create_artifact(self, task_id: str, name: str, content: str | None,
                              mime_type: str | None, parts: list | None = None) -> dict:
        artifact_id = _gen_id("art_")
        now = _now()
        if parts is None and content:
            part_type = "data" if mime_type and "json" in mime_type else "text"
            parts = [{"type": part_type, "content": content, "mime_type": mime_type}]
        elif parts is None:
            parts = []
        await self._db.execute("""
            INSERT INTO artifacts (artifact_id, task_id, name, parts, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (artifact_id, task_id, name, json.dumps(parts), now))
        await self._db.commit()
        await self.audit("artifact.created", task_id=task_id,
                         detail={"artifact_id": artifact_id, "name": name})
        return {
            "artifact_id": artifact_id,
            "task_id": task_id,
            "name": name,
            "parts": parts,
            "created_at": now,
        }

    async def get_artifacts(self, task_id: str) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at", (task_id,))
        rows = await cursor.fetchall()
        return [{
            "artifact_id": r["artifact_id"],
            "task_id": r["task_id"],
            "name": r["name"],
            "parts": json.loads(r["parts"]),
            "created_at": r["created_at"],
        } for r in rows]

    # --- Inbox ---

    async def get_inbox(self, agent_id: str, status: str | None = None,
                        limit: int = 50) -> dict:
        """Get pending tasks and unread messages for an agent."""
        # Pending tasks (submitted to this agent)
        pending_q = """
            SELECT * FROM tasks WHERE to_agent=? AND status IN ('submitted', 'input_needed')
            ORDER BY CASE priority
                WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                WHEN 'normal' THEN 2 WHEN 'low' THEN 3 END,
            created_at DESC LIMIT ?
        """
        cursor = await self._db.execute(pending_q, (agent_id, limit))
        pending = [self._row_to_task(r) for r in await cursor.fetchall()]

        # Unread messages (deliveries pending for this agent)
        unread_q = """
            SELECT m.*, d.delivery_id, t.title as task_title
            FROM deliveries d
            JOIN messages m ON d.message_id = m.message_id
            JOIN tasks t ON m.task_id = t.task_id
            WHERE d.target_agent=? AND d.status='pending' AND d.message_id IS NOT NULL
            ORDER BY m.created_at DESC LIMIT ?
        """
        cursor = await self._db.execute(unread_q, (agent_id, limit))
        unread_rows = await cursor.fetchall()
        unread = []
        for r in unread_rows:
            unread.append({
                "delivery_id": r["delivery_id"],
                "message_id": r["message_id"],
                "task_id": r["task_id"],
                "task_title": r["task_title"],
                "from_agent": r["from_agent"],
                "parts": json.loads(r["parts"]),
                "created_at": r["created_at"],
            })

        # Tasks needing input from this agent (as sender)
        input_q = """
            SELECT * FROM tasks WHERE from_agent=? AND status='input_needed'
            ORDER BY updated_at DESC LIMIT ?
        """
        cursor = await self._db.execute(input_q, (agent_id, limit))
        needs_input = [self._row_to_task(r) for r in await cursor.fetchall()]

        return {
            "pending_tasks": pending,
            "unread_messages": unread,
            "tasks_needing_input": needs_input,
        }

    async def acknowledge(self, agent_id: str, delivery_ids: list[str]) -> int:
        """Mark deliveries as acknowledged. Returns count acknowledged."""
        if not delivery_ids:
            return 0
        now = _now()
        placeholders = ",".join("?" * len(delivery_ids))
        cursor = await self._db.execute(f"""
            UPDATE deliveries SET status='acknowledged', acknowledged_at=?
            WHERE delivery_id IN ({placeholders}) AND target_agent=?
        """, [now] + delivery_ids + [agent_id])
        await self._db.commit()
        return cursor.rowcount

    # --- Broadcast ---

    async def broadcast(self, from_agent: str, content: str,
                        metadata: dict | None = None) -> list[str]:
        """Send a message to all agents. Returns list of delivery IDs."""
        agents = await self.list_agents()
        delivery_ids = []
        now = _now()
        for agent in agents:
            if agent["agent_id"] == from_agent:
                continue
            delivery_id = _gen_id("del_")
            # Store as a special "broadcast" task
            task_id = _gen_id("bcast_")
            await self._db.execute("""
                INSERT INTO tasks (task_id, title, description, from_agent, to_agent,
                    status, priority, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'completed', 'normal', ?, ?, ?)
            """, (task_id, f"Broadcast from {from_agent}", content, from_agent,
                  agent["agent_id"], json.dumps(metadata) if metadata else None, now, now))

            await self._db.execute("""
                INSERT INTO messages (message_id, task_id, from_agent, role, parts, created_at)
                VALUES (?, ?, ?, 'system', ?, ?)
            """, (_gen_id("msg_"), task_id, from_agent,
                  json.dumps([{"type": "text", "content": content}]), now))

            await self._db.execute("""
                INSERT INTO deliveries (delivery_id, task_id, target_agent, method,
                    status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
            """, (delivery_id, task_id, agent["agent_id"],
                  agent["contact"]["method"], now))
            delivery_ids.append(delivery_id)

        await self._db.commit()
        await self.audit("broadcast.sent", agent_id=from_agent,
                         detail={"recipients": len(delivery_ids), "content": content[:200]})
        return delivery_ids

    # --- Audit ---

    async def audit(self, event_type: str, agent_id: str | None = None,
                    task_id: str | None = None, detail: dict | None = None):
        await self._db.execute("""
            INSERT INTO audit_log (event_type, agent_id, task_id, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (event_type, agent_id, task_id,
              json.dumps(detail) if detail else None, _now()))
        await self._db.commit()

    async def get_audit_log(self, since: str | None = None, until: str | None = None,
                            agent_id: str | None = None, limit: int = 100) -> list[dict]:
        query = "SELECT * FROM audit_log WHERE 1=1"
        params = []
        if since:
            query += " AND created_at>=?"
            params.append(since)
        if until:
            query += " AND created_at<=?"
            params.append(until)
        if agent_id:
            query += " AND agent_id=?"
            params.append(agent_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self._db.execute(query, params)
        rows = await cursor.fetchall()
        return [{
            "log_id": r["log_id"],
            "event_type": r["event_type"],
            "agent_id": r["agent_id"],
            "task_id": r["task_id"],
            "detail": json.loads(r["detail"]) if r["detail"] else None,
            "created_at": r["created_at"],
        } for r in rows]

    # --- Stats ---

    async def stats(self) -> dict:
        agents = await self._db.execute("SELECT COUNT(*) as c FROM agents")
        agent_count = (await agents.fetchone())["c"]

        pending = await self._db.execute(
            "SELECT COUNT(*) as c FROM tasks WHERE status IN ('submitted', 'accepted', 'working', 'input_needed')")
        pending_count = (await pending.fetchone())["c"]

        total = await self._db.execute("SELECT COUNT(*) as c FROM tasks")
        total_count = (await total.fetchone())["c"]

        return {
            "agents": agent_count,
            "pending_tasks": pending_count,
            "total_tasks": total_count,
        }
