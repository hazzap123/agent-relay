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
        "agent_id": "primary",
        "name": "Primary Agent",
        "description": "Strategy and analysis",
        "capabilities": ["strategy", "analysis", "code"],
        "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })
    assert resp.status_code == 200
    agents["primary"] = resp.json()["api_key"]

    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "scheduler",
        "name": "Scheduler",
        "description": "Scheduling and admin",
        "capabilities": ["scheduling", "email"],
        "contact": {"method": "webhook", "webhook_url": "http://localhost:8080/webhook"},
        "trust_tier": 2,
        "permissions": {
            "can_read_from": ["primary"],
            "can_send_to": ["primary", "builder"],
        },
    })
    assert resp.status_code == 200
    agents["scheduler"] = resp.json()["api_key"]

    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "builder",
        "name": "Builder",
        "description": "Code execution",
        "capabilities": ["code-execution", "testing"],
        "trust_tier": 1,
        "permissions": {"can_read_from": ["*"], "can_send_to": ["*"]},
    })
    assert resp.status_code == 200
    agents["builder"] = resp.json()["api_key"]

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
    resp = await client.get("/api/v1/agents", headers=_h("primary"))
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert len(agents) == 3
    ids = {a["agent_id"] for a in agents}
    assert ids == {"primary", "scheduler", "builder"}


@pytest.mark.asyncio
async def test_heartbeat(client, registered_agents):
    resp = await client.post("/api/v1/agents/primary/heartbeat",
                             headers=_h("primary"),
                             json={"status": "online"})
    assert resp.status_code == 200

    resp = await client.get("/api/v1/agents/primary", headers=_h("primary"))
    assert resp.json()["status"] == "online"


# --- Task Lifecycle ---

@pytest.mark.asyncio
async def test_create_task(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "scheduler",
        "title": "Schedule meeting with Alice",
        "description": "30 min, next week, prefer Tue-Thu",
        "priority": "high",
    })
    assert resp.status_code == 200
    task = resp.json()
    assert task["from_agent"] == "primary"
    assert task["to_agent"] == "scheduler"
    assert task["status"] == "submitted"
    assert task["priority"] == "high"
    assert task["task_id"].startswith("task_")


@pytest.mark.asyncio
async def test_full_task_lifecycle(client, registered_agents):
    """Test: submit → accept → working → complete with message."""
    # Create
    resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "scheduler",
        "title": "Send follow-up email",
    })
    task_id = resp.json()["task_id"]

    # Accept
    resp = await client.patch(f"/api/v1/tasks/{task_id}", headers=_h("scheduler"), json={
        "status": "accepted",
    })
    assert resp.json()["status"] == "accepted"

    # Working
    resp = await client.patch(f"/api/v1/tasks/{task_id}", headers=_h("scheduler"), json={
        "status": "working",
    })
    assert resp.json()["status"] == "working"

    # Complete with message
    resp = await client.patch(f"/api/v1/tasks/{task_id}", headers=_h("scheduler"), json={
        "status": "completed",
        "message": "Email sent to alice@example.com at 14:35",
    })
    assert resp.json()["status"] == "completed"

    # Check message was created
    resp = await client.get(f"/api/v1/tasks/{task_id}/messages", headers=_h("primary"))
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert "alice@example.com" in msgs[0]["parts"][0]["content"]


@pytest.mark.asyncio
async def test_task_input_needed(client, registered_agents):
    """Test input_needed flow."""
    resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "scheduler",
        "title": "Reply to recruiter email",
    })
    task_id = resp.json()["task_id"]

    # Scheduler needs input
    resp = await client.patch(f"/api/v1/tasks/{task_id}", headers=_h("scheduler"), json={
        "status": "input_needed",
        "message": "Found two contacts — Alice (PM role) and Bob (Eng role). Which?",
    })
    assert resp.json()["status"] == "input_needed"

    # Check primary's inbox shows it
    resp = await client.get("/api/v1/inbox/primary", headers=_h("primary"))
    inbox = resp.json()
    assert len(inbox["tasks_needing_input"]) == 1
    assert inbox["tasks_needing_input"][0]["task_id"] == task_id


# --- Messages ---

@pytest.mark.asyncio
async def test_message_thread(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "scheduler",
        "title": "Test task",
    })
    task_id = resp.json()["task_id"]

    # Send messages back and forth
    await client.post(f"/api/v1/tasks/{task_id}/messages", headers=_h("scheduler"),
                      json={"content": "Got it, working on it"})
    await client.post(f"/api/v1/tasks/{task_id}/messages", headers=_h("primary"),
                      json={"content": "Great, thanks"})
    await client.post(f"/api/v1/tasks/{task_id}/messages", headers=_h("scheduler"),
                      json={"content": "Done!"})

    resp = await client.get(f"/api/v1/tasks/{task_id}/messages", headers=_h("primary"))
    msgs = resp.json()["messages"]
    assert len(msgs) == 3
    assert msgs[0]["from_agent"] == "scheduler"
    assert msgs[2]["from_agent"] == "scheduler"


# --- Inbox ---

@pytest.mark.asyncio
async def test_inbox_pending_tasks(client, registered_agents):
    # Send 2 tasks to scheduler
    for title in ["Task A", "Task B"]:
        await client.post("/api/v1/tasks", headers=_h("primary"),
                          json={"to_agent": "scheduler", "title": title})

    resp = await client.get("/api/v1/inbox/scheduler", headers=_h("scheduler"))
    inbox = resp.json()
    assert len(inbox["pending_tasks"]) == 2


@pytest.mark.asyncio
async def test_inbox_unread_messages(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("primary"),
                             json={"to_agent": "scheduler", "title": "Test"})
    task_id = resp.json()["task_id"]

    # Primary sends a message
    await client.post(f"/api/v1/tasks/{task_id}/messages", headers=_h("primary"),
                      json={"content": "Please check this"})

    # Scheduler checks inbox
    resp = await client.get("/api/v1/inbox/scheduler", headers=_h("scheduler"))
    inbox = resp.json()
    assert len(inbox["unread_messages"]) >= 1


# --- Trust & Permissions ---

@pytest.mark.asyncio
async def test_scheduler_cannot_read_other_inbox(client, registered_agents):
    resp = await client.get("/api/v1/inbox/primary", headers=_h("scheduler"))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_scheduler_cannot_send_to_unauthorised(client, registered_agents):
    """Scheduler can only send to primary and builder."""
    # Register a new agent that scheduler can't send to
    await client.post("/api/v1/agents/register", json={
        "agent_id": "secret-agent",
        "name": "Secret",
        "trust_tier": 1,
    })

    resp = await client.post("/api/v1/tasks", headers=_h("scheduler"), json={
        "to_agent": "secret-agent",
        "title": "Shouldn't work",
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_scheduler_cannot_read_others_tasks(client, registered_agents):
    """Scheduler (tier 2) can't read tasks between primary and builder."""
    resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "builder",
        "title": "Internal task",
    })
    task_id = resp.json()["task_id"]

    resp = await client.get(f"/api/v1/tasks/{task_id}", headers=_h("scheduler"))
    assert resp.status_code == 403


# --- Artifacts ---

@pytest.mark.asyncio
async def test_attach_artifact(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "scheduler",
        "title": "Schedule meeting",
    })
    task_id = resp.json()["task_id"]

    resp = await client.post(f"/api/v1/tasks/{task_id}/artifacts",
                             headers=_h("scheduler"), json={
        "name": "calendar_event",
        "content": '{"event_id": "abc123", "title": "Team Sync"}',
        "mime_type": "application/json",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "calendar_event"

    # Get artifacts
    resp = await client.get(f"/api/v1/tasks/{task_id}/artifacts",
                            headers=_h("primary"))
    arts = resp.json()["artifacts"]
    assert len(arts) == 1


# --- Broadcast ---

@pytest.mark.asyncio
async def test_broadcast(client, registered_agents):
    resp = await client.post("/api/v1/broadcast", headers=_h("primary"), json={
        "content": "System maintenance window next Tuesday",
        "metadata": {"type": "maintenance-notice"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["recipients"] == 2  # scheduler + builder (not self)


# --- Audit ---

@pytest.mark.asyncio
async def test_audit_log(client, registered_agents):
    # Create a task to generate audit entries
    await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "scheduler",
        "title": "Audit test",
    })

    resp = await client.get("/api/v1/audit", headers=_h("primary"))
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) > 0
    # Should have agent registration + task creation entries
    event_types = {e["event_type"] for e in entries}
    assert "task.created" in event_types


@pytest.mark.asyncio
async def test_audit_denied_for_tier2(client, registered_agents):
    resp = await client.get("/api/v1/audit", headers=_h("scheduler"))
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
        resp = await ac.get("/api/v1/agents", headers={"X-Agent-ID": "primary"})
        assert resp.status_code == 401

        # Bearer token should work
        key = registered_agents["primary"]
        resp = await ac.get("/api/v1/agents",
                            headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200

    # Reset for other tests
    app.state.auth_enabled = False


# --- Edge Cases ---

@pytest.mark.asyncio
async def test_task_not_found(client, registered_agents):
    resp = await client.get("/api/v1/tasks/nonexistent", headers=_h("primary"))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_send_to_nonexistent_agent(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "nobody",
        "title": "Should fail",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sender_can_cancel(client, registered_agents):
    resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "scheduler",
        "title": "Cancel me",
    })
    task_id = resp.json()["task_id"]

    resp = await client.patch(f"/api/v1/tasks/{task_id}", headers=_h("primary"), json={
        "status": "cancelled",
    })
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_get_task_with_messages(client, registered_agents):
    """Full task retrieval includes messages and artifacts."""
    resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "scheduler",
        "title": "Full task",
    })
    task_id = resp.json()["task_id"]

    await client.post(f"/api/v1/tasks/{task_id}/messages", headers=_h("scheduler"),
                      json={"content": "Working on it"})
    await client.post(f"/api/v1/tasks/{task_id}/artifacts", headers=_h("scheduler"),
                      json={"name": "result", "content": "Done"})

    resp = await client.get(f"/api/v1/tasks/{task_id}", headers=_h("primary"))
    data = resp.json()
    assert len(data["messages"]) == 1
    assert len(data["artifacts"]) == 1


# --- Re-registration Security ---

@pytest.mark.asyncio
async def test_reregister_without_auth_rejected(client):
    """Unauthenticated re-registration must be blocked (account takeover prevention)."""
    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "victim",
        "name": "Original Agent",
    })
    assert resp.status_code == 200

    # Try to re-register same ID without auth
    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "victim",
        "name": "Attacker",
    })
    assert resp.status_code == 409
    assert "already registered" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_reregister_with_correct_key_allowed(client):
    """Re-registration with the agent's own Bearer token should succeed."""
    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "self-update",
        "name": "Original",
    })
    api_key = resp.json()["api_key"]

    resp = await client.post("/api/v1/agents/register",
                             headers={"Authorization": f"Bearer {api_key}"},
                             json={
        "agent_id": "self-update",
        "name": "Updated Name",
    })
    assert resp.status_code == 200
    assert resp.json()["agent"]["name"] == "Updated Name"


@pytest.mark.asyncio
async def test_reregister_with_admin_allowed(client, registered_agents):
    """Tier-1 admin can re-register another agent."""
    admin_key = registered_agents["primary"]  # tier 1

    resp = await client.post("/api/v1/agents/register",
                             headers={"Authorization": f"Bearer {admin_key}"},
                             json={
        "agent_id": "scheduler",
        "name": "Scheduler Reregistered",
    })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_reregister_with_wrong_key_rejected(client, registered_agents):
    """Non-admin agent can't re-register a different agent."""
    scheduler_key = registered_agents["scheduler"]  # tier 2

    resp = await client.post("/api/v1/agents/register",
                             headers={"Authorization": f"Bearer {scheduler_key}"},
                             json={
        "agent_id": "primary",
        "name": "Hijacked",
    })
    assert resp.status_code == 403


# --- Webhook URL Validation ---

@pytest.mark.asyncio
async def test_webhook_url_bad_scheme_rejected(client):
    """file:// scheme must be rejected."""
    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "bad-scheme",
        "name": "Bad Scheme",
        "contact": {"method": "webhook", "webhook_url": "file:///etc/passwd"},
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_webhook_url_metadata_rejected(client):
    """Cloud metadata IP must be blocked."""
    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "ssrf",
        "name": "SSRF Agent",
        "contact": {"method": "webhook", "webhook_url": "http://169.254.169.254/latest/meta-data/"},
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_webhook_url_valid_accepted(client):
    """Valid localhost webhook should be accepted."""
    resp = await client.post("/api/v1/agents/register", json={
        "agent_id": "good-webhook",
        "name": "Good Webhook",
        "contact": {"method": "webhook", "webhook_url": "http://127.0.0.1:8080/webhook"},
    })
    assert resp.status_code == 200


# --- Webhook Event Dispatch (B5) ---

@pytest.mark.asyncio
async def test_webhook_event_payload_format(client, registered_agents):
    """Webhook payload must use {"event": "...", "data": {...}} structure."""
    import respx

    # Scheduler has a webhook_url registered
    with respx.mock:
        route = respx.post("http://localhost:8080/webhook").respond(200)

        resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
            "to_agent": "scheduler",
            "title": "Webhook format test",
        })
        assert resp.status_code == 200

        # Verify webhook was called with correct format
        assert route.called
        body = route.calls[0].request.content
        import json as _json
        payload = _json.loads(body)
        assert "event" in payload
        assert payload["event"] == "task.created"
        assert "data" in payload
        assert payload["data"]["title"] == "Webhook format test"


@pytest.mark.asyncio
async def test_webhook_on_message_new(client, registered_agents):
    """Webhook dispatched when a message is posted to a task thread."""
    import respx

    with respx.mock:
        route = respx.post("http://localhost:8080/webhook").respond(200)

        # Create task to scheduler (has webhook)
        resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
            "to_agent": "scheduler",
            "title": "Message webhook test",
        })
        task_id = resp.json()["task_id"]
        initial_calls = len(route.calls)

        # Primary sends message — should trigger webhook to scheduler
        resp = await client.post(f"/api/v1/tasks/{task_id}/messages",
                                 headers=_h("primary"),
                                 json={"content": "Check this out"})
        assert resp.status_code == 200

        assert len(route.calls) > initial_calls
        import json as _json
        last_payload = _json.loads(route.calls[-1].request.content)
        assert last_payload["event"] == "message.new"
        assert last_payload["data"]["from_agent"] == "primary"


@pytest.mark.asyncio
async def test_webhook_on_task_updated(client, registered_agents):
    """Webhook dispatched when task status changes."""
    import respx

    with respx.mock:
        route = respx.post("http://localhost:8080/webhook").respond(200)

        # Create task to scheduler
        resp = await client.post("/api/v1/tasks", headers=_h("primary"), json={
            "to_agent": "scheduler",
            "title": "Status webhook test",
        })
        task_id = resp.json()["task_id"]

        # Scheduler accepts — should trigger webhook to primary (but primary has no webhook)
        # So let's test the other direction: primary updates → scheduler gets webhook
        resp = await client.patch(f"/api/v1/tasks/{task_id}",
                                  headers=_h("primary"),
                                  json={"status": "cancelled"})
        assert resp.status_code == 200

        # Scheduler has webhook, primary cancelled → scheduler should get task.updated
        import json as _json
        task_updated_calls = [
            c for c in route.calls
            if _json.loads(c.request.content).get("event") == "task.updated"
        ]
        assert len(task_updated_calls) >= 1
        payload = _json.loads(task_updated_calls[0].request.content)
        assert payload["data"]["status"] == "cancelled"


# --- Inbox Filtering (D1) ---

@pytest.mark.asyncio
async def test_inbox_filter_by_from_agent(client, registered_agents):
    """Inbox filtered by from_agent only shows items from that sender."""
    # Send tasks from different agents to builder
    await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "builder", "title": "From primary"})
    await client.post("/api/v1/tasks", headers=_h("scheduler"), json={
        "to_agent": "builder", "title": "From scheduler"})

    # Filter by primary
    resp = await client.get("/api/v1/inbox/builder",
                            headers=_h("builder"),
                            params={"from": "primary"})
    assert resp.status_code == 200
    inbox = resp.json()
    for task in inbox["pending_tasks"]:
        assert task["from_agent"] == "primary"
    assert len(inbox["pending_tasks"]) == 1


@pytest.mark.asyncio
async def test_inbox_filter_by_since(client, registered_agents):
    """Inbox filtered by since only shows items after that date."""
    await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "scheduler", "title": "Old task"})

    # Use a future date — should return nothing
    resp = await client.get("/api/v1/inbox/scheduler",
                            headers=_h("scheduler"),
                            params={"since": "2099-01-01T00:00:00Z"})
    assert resp.status_code == 200
    inbox = resp.json()
    assert len(inbox["pending_tasks"]) == 0
    assert len(inbox["unread_messages"]) == 0


# --- Long Polling ---

@pytest.mark.asyncio
async def test_wait_inbox_returns_immediately_when_items(client, registered_agents):
    """Long poll returns immediately when inbox already has items."""
    await client.post("/api/v1/tasks", headers=_h("primary"), json={
        "to_agent": "scheduler", "title": "Waiting task"})

    resp = await client.get("/api/v1/inbox/scheduler/wait",
                            headers=_h("scheduler"),
                            params={"timeout": 5})
    assert resp.status_code == 200
    inbox = resp.json()
    assert len(inbox["pending_tasks"]) >= 1


@pytest.mark.asyncio
async def test_wait_inbox_timeout_returns_empty(client, registered_agents):
    """Long poll returns empty inbox after timeout when nothing arrives."""
    import time
    start = time.time()
    resp = await client.get("/api/v1/inbox/builder/wait",
                            headers=_h("builder"),
                            params={"timeout": 2})
    elapsed = time.time() - start
    assert resp.status_code == 200
    assert elapsed >= 1.5
    inbox = resp.json()
    assert len(inbox["pending_tasks"]) == 0


@pytest.mark.asyncio
async def test_wait_inbox_permission_denied(client, registered_agents):
    """Tier 2 agent can't long-poll another agent's inbox."""
    resp = await client.get("/api/v1/inbox/primary/wait",
                            headers=_h("scheduler"),
                            params={"timeout": 2})
    assert resp.status_code == 403
