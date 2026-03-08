"""
Tests for Agent Relay API.

Uses httpx async client with FastAPI's TestClient pattern.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay.db import Database
from relay.server import app


@pytest_asyncio.fixture
async def db(tmp_path):
    """Create a fresh in-memory database for each test."""
    db_path = str(tmp_path / "test_relay.db")
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def client(db):
    """Create test client with injected database."""
    app.state.db = db
    app.state.auth_enabled = False  # Disable auth for tests — use X-Agent-ID
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def registered_agents(client):
    """Register the 3 standard agents and return their API keys."""
    agents = {}

    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "claude-code",
        "name": "Claude Code",
        "description": "Chief of Staff",
        "capabilities": ["strategy", "analysis", "code"],
        "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })
    assert resp.status_code == 200
    agents["claude-code"] = resp.json()["api_key"]

    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "clawdia",
        "name": "Clawdia",
        "description": "Scheduling and admin",
        "capabilities": ["scheduling", "email"],
        "contact": {"method": "webhook", "webhook_url": "http://localhost:8080/webhook"},
        "trust_tier": 2,
        "permissions": {
            "can_read_from": ["claude-code"],
            "can_send_to": ["claude-code", "abby"],
        },
    })
    assert resp.status_code == 200
    agents["clawdia"] = resp.json()["api_key"]

    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "abby",
        "name": "Abby",
        "description": "Code execution",
        "capabilities": ["code-execution", "testing"],
        "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })
    assert resp.status_code == 200
    agents["abby"] = resp.json()["api_key"]

    return agents


def _h(agent_id: str) -> dict:
    """Headers for a given agent (X-Agent-ID mode)."""
    return {"X-Agent-ID": agent_id}


# --- Health ---

@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


# --- Agent Registration ---

@pytest.mark.asyncio
async def test_register_agent(client):
    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "test-agent",
        "name": "Test Agent",
        "capabilities": ["testing"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent"]["agent_id"] == "test-agent"
    assert "api_key" in data
    assert len(data["api_key"]) == 64  # 32 bytes hex


@pytest.mark.asyncio
async def test_list_agents(client, registered_agents):
    resp = await client.get("/api/v1/agents", headers=_h("claude-code"))
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert len(agents) == 3
    ids = {a["agent_id"] for a in agents}
    assert ids == {"claude-code", "clawdia", "abby"}


@pytest.mark.asyncio
async def test_heartbeat(client, registered_agents):
    resp = await client.post("/api/v1/agents/claude-code/heartbeat",
                             json={"status": "online"})
    assert resp.status_code == 200

    resp = await client.get("/api/v1/agents/claude-code", headers=_h("claude-code"))
    assert resp.json()["status"] == "online"


# --- Task Lifecycle ---

@pytest.mark.asyncio
async def test_create_task(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("claude-code"), json={
        "to_agent": "clawdia",
        "title": "Schedule meeting with David",
        "description": "30 min, next week, prefer Tue-Thu",
        "priority": "high",
    })
    assert resp.status_code == 200
    task = resp.json()
    assert task["from_agent"] == "claude-code"
    assert task["to_agent"] == "clawdia"
    assert task["status"] == "submitted"
    assert task["priority"] == "high"
    assert task["task_id"].startswith("task_")


@pytest.mark.asyncio
async def test_full_task_lifecycle(client, registered_agents):
    """Test: submit → accept → working → complete with message."""
    # Create
    resp = await client.post("/api/v1/tasks", headers=_h("claude-code"), json={
        "to_agent": "clawdia",
        "title": "Send follow-up email",
    })
    task_id = resp.json()["task_id"]

    # Accept
    resp = await client.patch(f"/api/v1/tasks/{task_id}", headers=_h("clawdia"), json={
        "status": "accepted",
    })
    assert resp.json()["status"] == "accepted"

    # Working
    resp = await client.patch(f"/api/v1/tasks/{task_id}", headers=_h("clawdia"), json={
        "status": "working",
    })
    assert resp.json()["status"] == "working"

    # Complete with message
    resp = await client.patch(f"/api/v1/tasks/{task_id}", headers=_h("clawdia"), json={
        "status": "completed",
        "message": "Email sent to jane@monzo.com at 14:35",
    })
    assert resp.json()["status"] == "completed"

    # Check message was created
    resp = await client.get(f"/api/v1/tasks/{task_id}/messages", headers=_h("claude-code"))
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert "jane@monzo.com" in msgs[0]["parts"][0]["content"]


@pytest.mark.asyncio
async def test_task_input_needed(client, registered_agents):
    """Test input_needed flow."""
    resp = await client.post("/api/v1/tasks", headers=_h("claude-code"), json={
        "to_agent": "clawdia",
        "title": "Reply to Stripe recruiter",
    })
    task_id = resp.json()["task_id"]

    # Clawdia needs input
    resp = await client.patch(f"/api/v1/tasks/{task_id}", headers=_h("clawdia"), json={
        "status": "input_needed",
        "message": "Two recruiters — Emma (PM) and Raj (Eng). Which?",
    })
    assert resp.json()["status"] == "input_needed"

    # Check Claude Code's inbox shows it
    resp = await client.get("/api/v1/inbox/claude-code", headers=_h("claude-code"))
    inbox = resp.json()
    assert len(inbox["tasks_needing_input"]) == 1
    assert inbox["tasks_needing_input"][0]["task_id"] == task_id


# --- Messages ---

@pytest.mark.asyncio
async def test_message_thread(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("claude-code"), json={
        "to_agent": "clawdia",
        "title": "Test task",
    })
    task_id = resp.json()["task_id"]

    # Send messages back and forth
    await client.post(f"/api/v1/tasks/{task_id}/messages", headers=_h("clawdia"),
                      json={"content": "Got it, working on it"})
    await client.post(f"/api/v1/tasks/{task_id}/messages", headers=_h("claude-code"),
                      json={"content": "Great, thanks"})
    await client.post(f"/api/v1/tasks/{task_id}/messages", headers=_h("clawdia"),
                      json={"content": "Done!"})

    resp = await client.get(f"/api/v1/tasks/{task_id}/messages", headers=_h("claude-code"))
    msgs = resp.json()["messages"]
    assert len(msgs) == 3
    assert msgs[0]["from_agent"] == "clawdia"
    assert msgs[2]["from_agent"] == "clawdia"


# --- Inbox ---

@pytest.mark.asyncio
async def test_inbox_pending_tasks(client, registered_agents):
    # Send 2 tasks to clawdia
    for title in ["Task A", "Task B"]:
        await client.post("/api/v1/tasks", headers=_h("claude-code"),
                          json={"to_agent": "clawdia", "title": title})

    resp = await client.get("/api/v1/inbox/clawdia", headers=_h("clawdia"))
    inbox = resp.json()
    assert len(inbox["pending_tasks"]) == 2


@pytest.mark.asyncio
async def test_inbox_unread_messages(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("claude-code"),
                             json={"to_agent": "clawdia", "title": "Test"})
    task_id = resp.json()["task_id"]

    # Claude sends a message
    await client.post(f"/api/v1/tasks/{task_id}/messages", headers=_h("claude-code"),
                      json={"content": "Please check this"})

    # Clawdia checks inbox
    resp = await client.get("/api/v1/inbox/clawdia", headers=_h("clawdia"))
    inbox = resp.json()
    assert len(inbox["unread_messages"]) >= 1


# --- Trust & Permissions ---

@pytest.mark.asyncio
async def test_clawdia_cannot_read_other_inbox(client, registered_agents):
    resp = await client.get("/api/v1/inbox/claude-code", headers=_h("clawdia"))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_clawdia_cannot_send_to_unauthorised(client, registered_agents):
    """Clawdia can only send to claude-code and abby."""
    # Register a new agent that Clawdia can't send to
    await client.post("/api/v1/agents/register", json={
        "agent_id": "secret-agent",
        "name": "Secret",
        "trust_tier": 1,
    })

    resp = await client.post("/api/v1/tasks", headers=_h("clawdia"), json={
        "to_agent": "secret-agent",
        "title": "Shouldn't work",
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_clawdia_cannot_read_others_tasks(client, registered_agents):
    """Clawdia (tier 2) can't read tasks between claude-code and abby."""
    resp = await client.post("/api/v1/tasks", headers=_h("claude-code"), json={
        "to_agent": "abby",
        "title": "Internal task",
    })
    task_id = resp.json()["task_id"]

    resp = await client.get(f"/api/v1/tasks/{task_id}", headers=_h("clawdia"))
    assert resp.status_code == 403


# --- Artifacts ---

@pytest.mark.asyncio
async def test_attach_artifact(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("claude-code"), json={
        "to_agent": "clawdia",
        "title": "Schedule meeting",
    })
    task_id = resp.json()["task_id"]

    resp = await client.post(f"/api/v1/tasks/{task_id}/artifacts",
                             headers=_h("clawdia"), json={
        "name": "calendar_event",
        "content": '{"event_id": "abc123", "title": "David Carroll"}',
        "mime_type": "application/json",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "calendar_event"

    # Get artifacts
    resp = await client.get(f"/api/v1/tasks/{task_id}/artifacts",
                            headers=_h("claude-code"))
    arts = resp.json()["artifacts"]
    assert len(arts) == 1


# --- Broadcast ---

@pytest.mark.asyncio
async def test_broadcast(client, registered_agents):
    resp = await client.post("/api/v1/broadcast", headers=_h("claude-code"), json={
        "content": "Harry is travelling next week",
        "metadata": {"type": "travel-notice"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["recipients"] == 2  # clawdia + abby (not self)


# --- Audit ---

@pytest.mark.asyncio
async def test_audit_log(client, registered_agents):
    # Create a task to generate audit entries
    await client.post("/api/v1/tasks", headers=_h("claude-code"), json={
        "to_agent": "clawdia",
        "title": "Audit test",
    })

    resp = await client.get("/api/v1/audit", headers=_h("claude-code"))
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) > 0
    # Should have agent registration + task creation entries
    event_types = {e["event_type"] for e in entries}
    assert "task.created" in event_types


@pytest.mark.asyncio
async def test_audit_denied_for_tier2(client, registered_agents):
    resp = await client.get("/api/v1/audit", headers=_h("clawdia"))
    assert resp.status_code == 403


# --- Bearer Token Auth ---

@pytest.mark.asyncio
async def test_bearer_auth(db, registered_agents):
    """Test that bearer token auth works when enabled."""
    app.state.db = db
    app.state.auth_enabled = True  # Enable auth

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # X-Agent-ID should be rejected
        resp = await ac.get("/api/v1/agents", headers={"X-Agent-ID": "claude-code"})
        assert resp.status_code == 401

        # Bearer token should work
        key = registered_agents["claude-code"]
        resp = await ac.get("/api/v1/agents",
                            headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200

    # Reset for other tests
    app.state.auth_enabled = False


# --- Edge Cases ---

@pytest.mark.asyncio
async def test_task_not_found(client, registered_agents):
    resp = await client.get("/api/v1/tasks/nonexistent", headers=_h("claude-code"))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_send_to_nonexistent_agent(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("claude-code"), json={
        "to_agent": "nobody",
        "title": "Should fail",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sender_can_cancel(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("claude-code"), json={
        "to_agent": "clawdia",
        "title": "Cancel me",
    })
    task_id = resp.json()["task_id"]

    resp = await client.patch(f"/api/v1/tasks/{task_id}", headers=_h("claude-code"), json={
        "status": "cancelled",
    })
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_get_task_with_messages(client, registered_agents):
    """Full task retrieval includes messages and artifacts."""
    resp = await client.post("/api/v1/tasks", headers=_h("claude-code"), json={
        "to_agent": "clawdia",
        "title": "Full task",
    })
    task_id = resp.json()["task_id"]

    await client.post(f"/api/v1/tasks/{task_id}/messages", headers=_h("clawdia"),
                      json={"content": "Working on it"})
    await client.post(f"/api/v1/tasks/{task_id}/artifacts", headers=_h("clawdia"),
                      json={"name": "result", "content": "Done"})

    resp = await client.get(f"/api/v1/tasks/{task_id}", headers=_h("claude-code"))
    data = resp.json()
    assert len(data["messages"]) == 1
    assert len(data["artifacts"]) == 1
