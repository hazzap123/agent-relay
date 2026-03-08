"""
End-to-end scenario test for Agent Relay.

Simulates a realistic multi-agent coordination flow:
1. Claude Code delegates scheduling to Clawdia (with input_needed round-trip)
2. Claude Code delegates implementation to Abby
3. Broadcast a travel notice to all agents
4. Verify inbox state at each stage

This is the test that proves the relay works before deploying to the Lenovo.
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
        "agent_id": "claude-code",
        "name": "Claude Code",
        "description": "Chief of Staff — strategy, deep work, analysis, code, memory system",
        "capabilities": ["strategy", "analysis", "code", "memory", "meeting-prep"],
        "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })
    assert resp.status_code == 200
    claude_key = resp.json()["api_key"]
    assert len(claude_key) == 64

    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "clawdia",
        "name": "Clawdia",
        "description": "Scheduling, follow-ups, email on behalf, admin tasks",
        "capabilities": ["scheduling", "email", "follow-ups", "whatsapp", "browser-research"],
        "contact": {"method": "webhook", "webhook_url": "http://localhost:8080/webhook/incoming"},
        "trust_tier": 2,
        "permissions": {
            "can_read_from": ["claude-code", "harry"],
            "can_send_to": ["claude-code", "abby"],
        },
    })
    assert resp.status_code == 200
    clawdia_key = resp.json()["api_key"]

    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "abby",
        "name": "Abby",
        "description": "Code execution, specs to shipped features, focused implementation",
        "capabilities": ["code-execution", "implementation", "testing", "deployment"],
        "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })
    assert resp.status_code == 200
    abby_key = resp.json()["api_key"]

    # Verify all agents visible
    resp = await client.get("/api/v1/agents", headers=_bearer(claude_key))
    agents = resp.json()["agents"]
    assert len(agents) == 3
    agent_ids = {a["agent_id"] for a in agents}
    assert agent_ids == {"claude-code", "clawdia", "abby"}

    # =========================================================================
    # Phase 2: Claude Code sends heartbeat (session start)
    # =========================================================================

    resp = await client.post("/api/v1/agents/claude-code/heartbeat",
                             json={"status": "online"})
    assert resp.status_code == 200

    # Check inbox is empty at start
    resp = await client.get("/api/v1/inbox/claude-code", headers=_bearer(claude_key))
    inbox = resp.json()
    assert len(inbox["pending_tasks"]) == 0
    assert len(inbox["unread_messages"]) == 0
    assert len(inbox["tasks_needing_input"]) == 0

    # =========================================================================
    # Phase 3: Delegate scheduling to Clawdia
    # =========================================================================

    resp = await client.post("/api/v1/tasks", headers=_bearer(claude_key), json={
        "to_agent": "clawdia",
        "title": "Schedule call with Firefish team",
        "description": "Schedule a 45-min call with the Firefish team for next week. "
                       "Check Harry's calendar for availability. Prefer afternoons.",
        "priority": "normal",
        "metadata": {"stream": "C", "context": "Firefish consulting engagement"},
    })
    assert resp.status_code == 200
    firefish_task = resp.json()
    firefish_id = firefish_task["task_id"]
    assert firefish_task["status"] == "submitted"
    assert firefish_task["from_agent"] == "claude-code"
    assert firefish_task["to_agent"] == "clawdia"

    # Clawdia's inbox should show the task
    resp = await client.get("/api/v1/inbox/clawdia", headers=_bearer(clawdia_key))
    inbox = resp.json()
    assert len(inbox["pending_tasks"]) == 1
    assert inbox["pending_tasks"][0]["task_id"] == firefish_id
    assert inbox["pending_tasks"][0]["title"] == "Schedule call with Firefish team"

    # =========================================================================
    # Phase 4: Clawdia accepts, then needs input
    # =========================================================================

    # Accept
    resp = await client.patch(f"/api/v1/tasks/{firefish_id}",
                              headers=_bearer(clawdia_key), json={
        "status": "accepted",
        "message": "On it. Checking Harry's calendar now.",
    })
    assert resp.json()["status"] == "accepted"

    # Clawdia needs input — multiple contacts at Firefish
    resp = await client.patch(f"/api/v1/tasks/{firefish_id}",
                              headers=_bearer(clawdia_key), json={
        "status": "input_needed",
        "message": "Found 3 contacts at Firefish: Mike (CEO), Sarah (CTO), "
                   "and James (PM). Which ones should be on the call?",
    })
    assert resp.json()["status"] == "input_needed"

    # Claude Code's inbox should show input_needed
    resp = await client.get("/api/v1/inbox/claude-code", headers=_bearer(claude_key))
    inbox = resp.json()
    assert len(inbox["tasks_needing_input"]) == 1
    assert inbox["tasks_needing_input"][0]["task_id"] == firefish_id

    # Claude Code should also have unread messages from Clawdia
    assert len(inbox["unread_messages"]) >= 1

    # =========================================================================
    # Phase 5: Claude Code replies, Clawdia completes with artifact
    # =========================================================================

    # Claude Code replies
    resp = await client.post(f"/api/v1/tasks/{firefish_id}/messages",
                             headers=_bearer(claude_key), json={
        "content": "Mike and Sarah. Not James — he's the day-to-day contact, "
                   "this is a strategic review.",
    })
    assert resp.status_code == 200

    # Clawdia sees the reply in her inbox
    resp = await client.get("/api/v1/inbox/clawdia", headers=_bearer(clawdia_key))
    inbox = resp.json()
    assert len(inbox["unread_messages"]) >= 1

    # Clawdia completes the task
    resp = await client.patch(f"/api/v1/tasks/{firefish_id}",
                              headers=_bearer(clawdia_key), json={
        "status": "completed",
        "message": "Scheduled: Firefish strategy call, Wed 12 March 14:00-14:45. "
                   "Attendees: Mike, Sarah, Harry. Calendar invite sent.",
    })
    assert resp.json()["status"] == "completed"

    # Attach calendar event artifact
    resp = await client.post(f"/api/v1/tasks/{firefish_id}/artifacts",
                             headers=_bearer(clawdia_key), json={
        "name": "calendar_event",
        "content": '{"event_id": "ff_call_123", "title": "Firefish Strategy Review", '
                   '"start": "2026-03-12T14:00:00Z", "end": "2026-03-12T14:45:00Z", '
                   '"attendees": ["mike@firefish.co.uk", "sarah@firefish.co.uk", '
                   '"harry@haiven.co.uk"]}',
        "mime_type": "application/json",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "calendar_event"

    # Verify full task with messages + artifacts
    resp = await client.get(f"/api/v1/tasks/{firefish_id}", headers=_bearer(claude_key))
    full_task = resp.json()
    assert full_task["status"] == "completed"
    assert len(full_task["messages"]) == 4  # accepted msg + input_needed msg + reply + completed msg
    assert len(full_task["artifacts"]) == 1
    assert full_task["artifacts"][0]["name"] == "calendar_event"

    # =========================================================================
    # Phase 6: Delegate implementation to Abby
    # =========================================================================

    resp = await client.post("/api/v1/tasks", headers=_bearer(claude_key), json={
        "to_agent": "abby",
        "title": "Implement people trust-tier API endpoint",
        "description": "Add GET /api/people/{email}/tier endpoint to the MCP server. "
                       "Returns trust tier for a given contact email. "
                       "See people.py for existing lookup logic.",
        "priority": "high",
        "metadata": {"stream": "A", "repo": "ea", "branch": "feature/people-api"},
    })
    assert resp.status_code == 200
    people_task = resp.json()
    people_id = people_task["task_id"]

    # Abby starts a session — heartbeat + inbox check
    resp = await client.post("/api/v1/agents/abby/heartbeat",
                             json={"status": "online"})
    assert resp.status_code == 200

    resp = await client.get("/api/v1/inbox/abby", headers=_bearer(abby_key))
    inbox = resp.json()
    assert len(inbox["pending_tasks"]) == 1
    assert inbox["pending_tasks"][0]["task_id"] == people_id
    assert inbox["pending_tasks"][0]["priority"] == "high"

    # Abby accepts and works
    resp = await client.patch(f"/api/v1/tasks/{people_id}",
                              headers=_bearer(abby_key), json={
        "status": "accepted",
    })
    assert resp.json()["status"] == "accepted"

    resp = await client.patch(f"/api/v1/tasks/{people_id}",
                              headers=_bearer(abby_key), json={
        "status": "working",
        "message": "Found people.py lookup logic. Implementing endpoint now.",
    })
    assert resp.json()["status"] == "working"

    # Abby completes with artifact
    resp = await client.patch(f"/api/v1/tasks/{people_id}",
                              headers=_bearer(abby_key), json={
        "status": "completed",
        "message": "Implemented in commit abc123 on branch feature/people-api. "
                   "4 tests passing. Ready for review.",
    })
    assert resp.json()["status"] == "completed"

    resp = await client.post(f"/api/v1/tasks/{people_id}/artifacts",
                             headers=_bearer(abby_key), json={
        "name": "implementation_result",
        "content": '{"commit": "abc123", "branch": "feature/people-api", '
                   '"tests": "4/4 passing", "files_changed": '
                   '["mcp_server.py", "people.py", "tests/test_people.py"]}',
        "mime_type": "application/json",
    })
    assert resp.status_code == 200

    # Abby goes offline
    resp = await client.post("/api/v1/agents/abby/heartbeat",
                             json={"status": "offline"})
    assert resp.status_code == 200

    # =========================================================================
    # Phase 7: Broadcast travel notice
    # =========================================================================

    resp = await client.post("/api/v1/broadcast", headers=_bearer(claude_key), json={
        "content": "Harry is travelling Mon-Wed next week. No meetings before 10:00. "
                   "All scheduling requests should account for timezone shift (+1h, CET).",
        "metadata": {"type": "travel-notice", "valid_until": "2026-03-12"},
    })
    assert resp.status_code == 200
    broadcast_result = resp.json()
    assert broadcast_result["recipients"] == 2  # clawdia + abby (not self)

    # =========================================================================
    # Phase 8: Verify final state
    # =========================================================================

    # Claude Code's inbox — should see completed task notifications from Abby
    resp = await client.get("/api/v1/inbox/claude-code", headers=_bearer(claude_key))
    inbox = resp.json()
    # No pending tasks (both completed)
    assert len(inbox["pending_tasks"]) == 0
    assert len(inbox["tasks_needing_input"]) == 0
    # Should have unread messages from Abby's updates
    assert len(inbox["unread_messages"]) >= 1

    # List all tasks — should see both
    resp = await client.get("/api/v1/tasks", headers=_bearer(claude_key))
    all_tasks = resp.json()["tasks"]
    # Filter out broadcast tasks
    real_tasks = [t for t in all_tasks if not t["task_id"].startswith("bcast_")]
    assert len(real_tasks) == 2

    completed_tasks = [t for t in real_tasks if t["status"] == "completed"]
    assert len(completed_tasks) == 2

    # Agent status check
    resp = await client.get("/api/v1/agents", headers=_bearer(claude_key))
    agents = {a["agent_id"]: a for a in resp.json()["agents"]}
    assert agents["claude-code"]["status"] == "online"
    assert agents["abby"]["status"] == "offline"

    # Audit log — should have comprehensive trail
    resp = await client.get("/api/v1/audit", headers=_bearer(claude_key))
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

    # Clawdia (Tier 2) cannot read Abby's task
    resp = await client.get(f"/api/v1/tasks/{people_id}",
                            headers=_bearer(clawdia_key))
    assert resp.status_code == 403

    # Clawdia cannot read Claude Code's inbox
    resp = await client.get("/api/v1/inbox/claude-code",
                            headers=_bearer(clawdia_key))
    assert resp.status_code == 403

    # Clawdia cannot access audit log
    resp = await client.get("/api/v1/audit", headers=_bearer(clawdia_key))
    assert resp.status_code == 403

    # Clawdia CAN read her own completed task
    resp = await client.get(f"/api/v1/tasks/{firefish_id}",
                            headers=_bearer(clawdia_key))
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
