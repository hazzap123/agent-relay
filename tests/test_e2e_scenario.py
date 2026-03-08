"""
End-to-end scenario test for Agent Relay.

Simulates a realistic multi-agent coordination flow:
1. Primary agent delegates scheduling to Scheduler (with input_needed round-trip)
2. Primary agent delegates implementation to Builder
3. Broadcast a notice to all agents
4. Verify inbox state at each stage
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay.db import Database
from relay.server import app


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "e2e_relay.db"))
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def client(db):
    app.state.db = db
    app.state.auth_enabled = True  # Use bearer auth like production
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _agent_id(agent_id: str) -> dict:
    """Fallback header for unauthenticated endpoints."""
    return {"X-Agent-ID": agent_id}


@pytest.mark.asyncio
async def test_full_e2e_scenario(client):
    """
    Complete scenario: registration → delegation → input_needed → completion → broadcast.
    Tests the full A2A-aligned task lifecycle across 3 agents.
    """

    # =========================================================================
    # Phase 1: Register all 3 agents
    # =========================================================================

    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "primary",
        "name": "Primary Agent",
        "description": "Strategy, analysis, code, and memory",
        "capabilities": ["strategy", "analysis", "code", "memory", "meeting-prep"],
        "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })
    assert resp.status_code == 200
    primary_key = resp.json()["api_key"]
    assert len(primary_key) == 64

    resp = await client.post("/api/v1/agents/register",
                             headers=_bearer(primary_key), json={
        "agent_id": "scheduler",
        "name": "Scheduler",
        "description": "Scheduling, follow-ups, email, admin tasks",
        "capabilities": ["scheduling", "email", "follow-ups", "browser-research"],
        "contact": {"method": "webhook", "webhook_url": "http://localhost:8080/webhook/incoming"},
        "trust_tier": 2,
        "permissions": {
            "can_read_from": ["primary"],
            "can_send_to": ["primary", "builder"],
        },
    })
    assert resp.status_code == 200
    scheduler_key = resp.json()["api_key"]

    resp = await client.post("/api/v1/agents/register",
                             headers=_bearer(primary_key), json={
        "agent_id": "builder",
        "name": "Builder",
        "description": "Code execution, specs to shipped features",
        "capabilities": ["code-execution", "implementation", "testing", "deployment"],
        "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })
    assert resp.status_code == 200
    builder_key = resp.json()["api_key"]

    # Verify all agents visible
    resp = await client.get("/api/v1/agents", headers=_bearer(primary_key))
    agents = resp.json()["agents"]
    assert len(agents) == 3
    agent_ids = {a["agent_id"] for a in agents}
    assert agent_ids == {"primary", "scheduler", "builder"}

    # =========================================================================
    # Phase 2: Primary agent sends heartbeat (session start)
    # =========================================================================

    resp = await client.post("/api/v1/agents/primary/heartbeat",
                             headers=_bearer(primary_key),
                             json={"status": "online"})
    assert resp.status_code == 200

    # Check inbox is empty at start
    resp = await client.get("/api/v1/inbox/primary", headers=_bearer(primary_key))
    inbox = resp.json()
    assert len(inbox["pending_tasks"]) == 0
    assert len(inbox["unread_messages"]) == 0
    assert len(inbox["tasks_needing_input"]) == 0

    # =========================================================================
    # Phase 3: Delegate scheduling to Scheduler
    # =========================================================================

    resp = await client.post("/api/v1/tasks", headers=_bearer(primary_key), json={
        "to_agent": "scheduler",
        "title": "Schedule call with Acme Corp team",
        "description": "Schedule a 45-min call with the Acme Corp team for next week. "
                       "Check calendar for availability. Prefer afternoons.",
        "priority": "normal",
        "metadata": {"stream": "C", "context": "Consulting engagement"},
    })
    assert resp.status_code == 200
    acme_task = resp.json()
    acme_id = acme_task["task_id"]
    assert acme_task["status"] == "submitted"
    assert acme_task["from_agent"] == "primary"
    assert acme_task["to_agent"] == "scheduler"

    # Scheduler's inbox should show the task
    resp = await client.get("/api/v1/inbox/scheduler", headers=_bearer(scheduler_key))
    inbox = resp.json()
    assert len(inbox["pending_tasks"]) == 1
    assert inbox["pending_tasks"][0]["task_id"] == acme_id
    assert inbox["pending_tasks"][0]["title"] == "Schedule call with Acme Corp team"

    # =========================================================================
    # Phase 4: Scheduler accepts, then needs input
    # =========================================================================

    # Accept
    resp = await client.patch(f"/api/v1/tasks/{acme_id}",
                              headers=_bearer(scheduler_key), json={
        "status": "accepted",
        "message": "On it. Checking calendar now.",
    })
    assert resp.json()["status"] == "accepted"

    # Scheduler needs input — multiple contacts
    resp = await client.patch(f"/api/v1/tasks/{acme_id}",
                              headers=_bearer(scheduler_key), json={
        "status": "input_needed",
        "message": "Found 3 contacts at Acme Corp: Alice (CEO), Bob (CTO), "
                   "and Carol (PM). Which ones should be on the call?",
    })
    assert resp.json()["status"] == "input_needed"

    # Primary's inbox should show input_needed
    resp = await client.get("/api/v1/inbox/primary", headers=_bearer(primary_key))
    inbox = resp.json()
    assert len(inbox["tasks_needing_input"]) == 1
    assert inbox["tasks_needing_input"][0]["task_id"] == acme_id

    # Primary should also have unread messages from Scheduler
    assert len(inbox["unread_messages"]) >= 1

    # =========================================================================
    # Phase 5: Primary replies, Scheduler completes with artifact
    # =========================================================================

    # Primary replies
    resp = await client.post(f"/api/v1/tasks/{acme_id}/messages",
                             headers=_bearer(primary_key), json={
        "content": "Alice and Bob. Not Carol — she's the day-to-day contact, "
                   "this is a strategic review.",
    })
    assert resp.status_code == 200

    # Scheduler sees the reply in inbox
    resp = await client.get("/api/v1/inbox/scheduler", headers=_bearer(scheduler_key))
    inbox = resp.json()
    assert len(inbox["unread_messages"]) >= 1

    # Scheduler completes the task
    resp = await client.patch(f"/api/v1/tasks/{acme_id}",
                              headers=_bearer(scheduler_key), json={
        "status": "completed",
        "message": "Scheduled: Acme strategy call, Wed 12 March 14:00-14:45. "
                   "Attendees: Alice, Bob, and you. Calendar invite sent.",
    })
    assert resp.json()["status"] == "completed"

    # Attach calendar event artifact
    resp = await client.post(f"/api/v1/tasks/{acme_id}/artifacts",
                             headers=_bearer(scheduler_key), json={
        "name": "calendar_event",
        "content": '{"event_id": "acme_call_123", "title": "Acme Strategy Review", '
                   '"start": "2026-03-12T14:00:00Z", "end": "2026-03-12T14:45:00Z", '
                   '"attendees": ["alice@example.com", "bob@example.com", '
                   '"user@example.com"]}',
        "mime_type": "application/json",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "calendar_event"

    # Verify full task with messages + artifacts
    resp = await client.get(f"/api/v1/tasks/{acme_id}", headers=_bearer(primary_key))
    full_task = resp.json()
    assert full_task["status"] == "completed"
    assert len(full_task["messages"]) == 4  # accepted msg + input_needed msg + reply + completed msg
    assert len(full_task["artifacts"]) == 1
    assert full_task["artifacts"][0]["name"] == "calendar_event"

    # =========================================================================
    # Phase 6: Delegate implementation to Builder
    # =========================================================================

    resp = await client.post("/api/v1/tasks", headers=_bearer(primary_key), json={
        "to_agent": "builder",
        "title": "Implement contact trust-tier API endpoint",
        "description": "Add GET /api/contacts/{email}/tier endpoint to the server. "
                       "Returns trust tier for a given contact email. "
                       "See contacts.py for existing lookup logic.",
        "priority": "high",
        "metadata": {"stream": "A", "repo": "my-project", "branch": "feature/contacts-api"},
    })
    assert resp.status_code == 200
    contacts_task = resp.json()
    contacts_id = contacts_task["task_id"]

    # Builder starts a session — heartbeat + inbox check
    resp = await client.post("/api/v1/agents/builder/heartbeat",
                             headers=_bearer(builder_key),
                             json={"status": "online"})
    assert resp.status_code == 200

    resp = await client.get("/api/v1/inbox/builder", headers=_bearer(builder_key))
    inbox = resp.json()
    assert len(inbox["pending_tasks"]) == 1
    assert inbox["pending_tasks"][0]["task_id"] == contacts_id
    assert inbox["pending_tasks"][0]["priority"] == "high"

    # Builder accepts and works
    resp = await client.patch(f"/api/v1/tasks/{contacts_id}",
                              headers=_bearer(builder_key), json={
        "status": "accepted",
    })
    assert resp.json()["status"] == "accepted"

    resp = await client.patch(f"/api/v1/tasks/{contacts_id}",
                              headers=_bearer(builder_key), json={
        "status": "working",
        "message": "Found contacts.py lookup logic. Implementing endpoint now.",
    })
    assert resp.json()["status"] == "working"

    # Builder completes with artifact
    resp = await client.patch(f"/api/v1/tasks/{contacts_id}",
                              headers=_bearer(builder_key), json={
        "status": "completed",
        "message": "Implemented in commit abc123 on branch feature/contacts-api. "
                   "4 tests passing. Ready for review.",
    })
    assert resp.json()["status"] == "completed"

    resp = await client.post(f"/api/v1/tasks/{contacts_id}/artifacts",
                             headers=_bearer(builder_key), json={
        "name": "implementation_result",
        "content": '{"commit": "abc123", "branch": "feature/contacts-api", '
                   '"tests": "4/4 passing", "files_changed": '
                   '["server.py", "contacts.py", "tests/test_contacts.py"]}',
        "mime_type": "application/json",
    })
    assert resp.status_code == 200

    # Builder goes offline
    resp = await client.post("/api/v1/agents/builder/heartbeat",
                             headers=_bearer(builder_key),
                             json={"status": "offline"})
    assert resp.status_code == 200

    # =========================================================================
    # Phase 7: Broadcast notice
    # =========================================================================

    resp = await client.post("/api/v1/broadcast", headers=_bearer(primary_key), json={
        "content": "Travelling Mon-Wed next week. No meetings before 10:00. "
                   "All scheduling requests should account for timezone shift (+1h, CET).",
        "metadata": {"type": "travel-notice", "valid_until": "2026-03-12"},
    })
    assert resp.status_code == 200
    broadcast_result = resp.json()
    assert broadcast_result["recipients"] == 2  # scheduler + builder (not self)

    # =========================================================================
    # Phase 8: Verify final state
    # =========================================================================

    # Primary's inbox — should see completed task notifications from Builder
    resp = await client.get("/api/v1/inbox/primary", headers=_bearer(primary_key))
    inbox = resp.json()
    # No pending tasks (both completed)
    assert len(inbox["pending_tasks"]) == 0
    assert len(inbox["tasks_needing_input"]) == 0
    # Should have unread messages from Builder's updates
    assert len(inbox["unread_messages"]) >= 1

    # List all tasks — should see both
    resp = await client.get("/api/v1/tasks", headers=_bearer(primary_key))
    all_tasks = resp.json()["tasks"]
    # Filter out broadcast tasks
    real_tasks = [t for t in all_tasks if not t["task_id"].startswith("bcast_")]
    assert len(real_tasks) == 2

    completed_tasks = [t for t in real_tasks if t["status"] == "completed"]
    assert len(completed_tasks) == 2

    # Agent status check
    resp = await client.get("/api/v1/agents", headers=_bearer(primary_key))
    agents = {a["agent_id"]: a for a in resp.json()["agents"]}
    assert agents["primary"]["status"] == "online"
    assert agents["builder"]["status"] == "offline"

    # Audit log — should have comprehensive trail
    resp = await client.get("/api/v1/audit", headers=_bearer(primary_key))
    entries = resp.json()["entries"]
    event_types = {e["event_type"] for e in entries}
    assert "agent.registered" in event_types
    assert "task.created" in event_types
    assert "task.updated" in event_types
    assert "message.sent" in event_types
    assert "artifact.created" in event_types
    assert "broadcast.sent" in event_types

    # =========================================================================
    # Phase 9: Trust boundary verification
    # =========================================================================

    # Scheduler (Tier 2) cannot read Builder's task
    resp = await client.get(f"/api/v1/tasks/{contacts_id}",
                            headers=_bearer(scheduler_key))
    assert resp.status_code == 403

    # Scheduler cannot read Primary's inbox
    resp = await client.get("/api/v1/inbox/primary",
                            headers=_bearer(scheduler_key))
    assert resp.status_code == 403

    # Scheduler cannot access audit log
    resp = await client.get("/api/v1/audit", headers=_bearer(scheduler_key))
    assert resp.status_code == 403

    # Scheduler CAN read their own completed task
    resp = await client.get(f"/api/v1/tasks/{acme_id}",
                            headers=_bearer(scheduler_key))
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_priority_ordering_in_inbox(client):
    """Verify inbox sorts by priority: urgent > high > normal > low."""
    # Register agents
    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "sender", "name": "Sender", "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })
    sender_key = resp.json()["api_key"]

    await client.post("/api/v1/agents/register", json={
        "agent_id": "receiver", "name": "Receiver", "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })

    # Send tasks in reverse priority order
    for priority in ["low", "normal", "high", "urgent"]:
        await client.post("/api/v1/tasks", headers=_bearer(sender_key), json={
            "to_agent": "receiver",
            "title": f"{priority} task",
            "priority": priority,
        })

    # Check inbox ordering
    resp = await client.get("/api/v1/inbox/receiver", headers=_bearer(sender_key))
    tasks = resp.json()["pending_tasks"]
    priorities = [t["priority"] for t in tasks]
    assert priorities == ["urgent", "high", "normal", "low"]


@pytest.mark.asyncio
async def test_acknowledge_clears_unread(client):
    """Verify acknowledging deliveries removes them from unread."""
    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "a1", "name": "A1", "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })
    a1_key = resp.json()["api_key"]

    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "a2", "name": "A2", "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })
    a2_key = resp.json()["api_key"]

    # Create task and send message
    resp = await client.post("/api/v1/tasks", headers=_bearer(a1_key), json={
        "to_agent": "a2", "title": "Ack test",
    })
    task_id = resp.json()["task_id"]

    await client.post(f"/api/v1/tasks/{task_id}/messages",
                      headers=_bearer(a1_key), json={"content": "Check this"})

    # A2 checks inbox — has unread
    resp = await client.get("/api/v1/inbox/a2", headers=_bearer(a2_key))
    unread = resp.json()["unread_messages"]
    assert len(unread) >= 1

    # Acknowledge
    delivery_ids = [m["delivery_id"] for m in unread]
    resp = await client.post("/api/v1/inbox/a2/ack", headers=_bearer(a2_key),
                             json={"delivery_ids": delivery_ids})
    assert resp.json()["acknowledged"] == len(delivery_ids)

    # Check again — should be empty
    resp = await client.get("/api/v1/inbox/a2", headers=_bearer(a2_key))
    assert len(resp.json()["unread_messages"]) == 0
