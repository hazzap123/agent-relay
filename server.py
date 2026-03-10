"""
Agent Relay — FastAPI server.

A2A-compatible message relay for CLI-based AI agents.
Run: python -m relay.server
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("relay")

from .auth import (
    check_read_permission,
    check_send_permission,
    check_task_update_permission,
    get_authenticated_agent,
)
from .config import AUTH_ENABLED, BOOTSTRAP_TOKEN, DB_PATH, HOST, PORT
from .db import Database
from .models import (
    AcknowledgeRequest,
    AgentRegisterRequest,
    ArtifactCreateRequest,
    BroadcastRequest,
    HeartbeatRequest,
    MessageCreateRequest,
    TaskCreateRequest,
    TaskUpdateRequest,
)

_start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(DB_PATH)
    await db.connect()
    app.state.db = db
    app.state.auth_enabled = AUTH_ENABLED
    yield
    await db.close()


app = FastAPI(
    title="Agent Relay",
    description="A2A-compatible message relay for CLI-based AI agents",
    version="0.1.0",
    lifespan=lifespan,
)


async def _dispatch_webhook(url: str, payload: dict, event: str = "task.created"):
    """Fire-and-forget POST to an agent's webhook URL."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"event": event, "data": payload})
        logger.info("Webhook [%s] dispatched to %s", event, url[:50])
    except Exception as e:
        logger.warning("Webhook dispatch failed for %s: %s", url, e)


# --- Health ---

@app.get("/health")
async def health(request: Request):
    db: Database = request.app.state.db
    s = await db.stats()
    return {
        "status": "ok",
        "agents": s["agents"],
        "pending_tasks": s["pending_tasks"],
        "uptime_seconds": round(time.time() - _start_time, 1),
    }


# --- Agents ---

@app.post("/api/v1/agents/register")
async def register_agent(body: AgentRegisterRequest, request: Request):
    db: Database = request.app.state.db

    # Cap self-registration at tier 2 unless:
    # 1. An existing tier-1 agent authenticates (admin vouch), or
    # 2. No agents exist yet AND RELAY_BOOTSTRAP_TOKEN matches (first agent)
    # Block re-registration without auth (prevents account takeover)
    existing = await db.get_agent(body.agent_id)
    if existing:
        try:
            caller = await get_authenticated_agent(request)
            is_self = await db.verify_api_key(
                body.agent_id,
                request.headers.get("authorization", "")[7:])
            is_admin = caller.get("trust_tier", 3) == 1
            if not is_self and not is_admin:
                raise HTTPException(
                    status_code=403,
                    detail="Re-registration requires auth as the existing agent or tier-1 admin")
        except HTTPException as e:
            if e.status_code == 401:
                raise HTTPException(
                    status_code=409, detail="Agent already registered")
            raise

    effective_tier = body.trust_tier
    if effective_tier < 2:
        agents = await db.list_agents()
        if not agents:
            # Bootstrap — require token if configured
            if BOOTSTRAP_TOKEN:
                if body.api_key != BOOTSTRAP_TOKEN:
                    raise HTTPException(
                        status_code=403,
                        detail="Tier 1 bootstrap requires RELAY_BOOTSTRAP_TOKEN")
            # else: no token configured, allow first agent (backwards compat)
        else:
            # Not bootstrap — require admin auth
            try:
                caller = await get_authenticated_agent(request)
                if caller.get("trust_tier", 3) > 1:
                    effective_tier = 2  # Non-admin caller can't grant tier 1
            except HTTPException:
                effective_tier = 2  # No auth header = cap at tier 2

    agent, api_key = await db.register_agent(
        agent_id=body.agent_id,
        name=body.name,
        description=body.description,
        version=body.version,
        capabilities=body.capabilities,
        contact_method=body.contact.method.value,
        webhook_url=body.contact.webhook_url,
        trust_tier=effective_tier,
        permissions=body.permissions.model_dump(),
        metadata=body.metadata,
        api_key=body.api_key,
    )
    return {"agent": agent, "api_key": api_key}


@app.get("/api/v1/agents")
async def list_agents(request: Request):
    agent = await get_authenticated_agent(request)
    db: Database = request.app.state.db
    agents = await db.list_agents()
    # Strip sensitive fields
    for a in agents:
        a.pop("api_key_hash", None)
    return {"agents": agents}


@app.get("/api/v1/agents/{agent_id}")
async def get_agent(agent_id: str, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db
    agent = await db.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@app.post("/api/v1/agents/{agent_id}/heartbeat")
async def heartbeat(agent_id: str, body: HeartbeatRequest, request: Request):
    db: Database = request.app.state.db

    # Authenticate: caller must be the agent sending the heartbeat
    caller = await get_authenticated_agent(request)
    if caller["agent_id"] != agent_id and caller.get("trust_tier", 3) > 1:
        raise HTTPException(status_code=403, detail="Can only heartbeat as yourself")

    success = await db.heartbeat(agent_id, body.status.value)
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "ok"}


@app.delete("/api/v1/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request):
    caller = await get_authenticated_agent(request)
    if caller.get("trust_tier", 3) > 1 and caller["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="Only Tier 1 agents can delete other agents")
    db: Database = request.app.state.db
    success = await db.delete_agent(agent_id)
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "deleted"}


# --- Tasks ---

@app.post("/api/v1/tasks")
async def create_task(body: TaskCreateRequest, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    # Check target agent exists
    target = await db.get_agent(body.to_agent)
    if not target:
        raise HTTPException(status_code=404, detail="Target agent not found")

    # Check permissions
    check_send_permission(caller, body.to_agent)

    task = await db.create_task(
        from_agent=caller["agent_id"],
        to_agent=body.to_agent,
        title=body.title,
        description=body.description,
        priority=body.priority.value,
        due_by=body.due_by,
        metadata=body.metadata,
    )

    # Dispatch webhook if target agent has one registered
    if target.get("contact", {}).get("webhook_url"):
        await _dispatch_webhook(target["contact"]["webhook_url"], task, "task.created")

    return task


@app.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db
    task = await db.get_task_with_messages(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    check_read_permission(caller, task)
    return task


@app.patch("/api/v1/tasks/{task_id}")
async def update_task(task_id: str, body: TaskUpdateRequest, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    check_task_update_permission(caller, task, body.status.value)

    updated = await db.update_task_status(task_id, body.status.value, caller["agent_id"])

    # If a message was included with the status update, create it
    if body.message:
        await db.create_message(task_id, caller["agent_id"], body.message)

    # Dispatch webhook to the other party on status change
    other_id = task["to_agent"] if caller["agent_id"] == task["from_agent"] else task["from_agent"]
    other = await db.get_agent(other_id)
    if other and other.get("contact", {}).get("webhook_url"):
        await _dispatch_webhook(other["contact"]["webhook_url"], updated, "task.updated")

    return updated


@app.get("/api/v1/tasks")
async def list_tasks(
    request: Request,
    to: str = Query(None),
    from_agent: str = Query(None, alias="from"),
    status: str = Query(None),
    since: str = Query(None),
    limit: int = Query(50, le=200),
):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    # Non-tier-1 agents can only see their own tasks
    if caller.get("trust_tier", 3) > 1:
        if to and to != caller["agent_id"] and from_agent != caller["agent_id"]:
            raise HTTPException(status_code=403, detail="Can only query own tasks")
        if not to and not from_agent:
            to = caller["agent_id"]

    tasks = await db.list_tasks(to_agent=to, from_agent=from_agent,
                                status=status, since=since, limit=limit)
    return {"tasks": tasks}


# --- Messages ---

@app.post("/api/v1/tasks/{task_id}/messages")
async def create_message(task_id: str, body: MessageCreateRequest, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    check_read_permission(caller, task)

    parts = [p.model_dump() for p in body.parts] if body.parts else None
    message = await db.create_message(task_id, caller["agent_id"], body.content, parts)

    # Dispatch webhook to the other party on new message
    target_id = task["to_agent"] if caller["agent_id"] == task["from_agent"] else task["from_agent"]
    target_agent = await db.get_agent(target_id)
    if target_agent and target_agent.get("contact", {}).get("webhook_url"):
        await _dispatch_webhook(target_agent["contact"]["webhook_url"], message, "message.new")

    return message


@app.get("/api/v1/tasks/{task_id}/messages")
async def get_messages(task_id: str, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    check_read_permission(caller, task)

    messages = await db.get_messages(task_id)
    return {"messages": messages}


# --- Inbox ---

@app.get("/api/v1/inbox/{agent_id}")
async def get_inbox(
    agent_id: str, request: Request,
    from_agent: str = Query(None, alias="from"),
    since: str = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    caller = await get_authenticated_agent(request)

    # Non-tier-1 can only check own inbox
    if caller.get("trust_tier", 3) > 1 and caller["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="Can only check own inbox")

    db: Database = request.app.state.db
    inbox = await db.get_inbox(agent_id, from_agent=from_agent, since=since,
                               limit=limit, offset=offset)
    return inbox


@app.get("/api/v1/inbox/{agent_id}/wait")
async def wait_for_inbox(
    agent_id: str, request: Request,
    timeout: int = Query(30, le=60),
    from_agent: str = Query(None, alias="from"),
    since: str = Query(None),
):
    """Long-poll: block until new inbox items arrive or timeout."""
    caller = await get_authenticated_agent(request)
    if caller.get("trust_tier", 3) > 1 and caller["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="Can only check own inbox")

    db: Database = request.app.state.db

    for _ in range(timeout // 2):
        inbox = await db.get_inbox(agent_id, from_agent=from_agent, since=since, limit=20)
        if inbox["pending_tasks"] or inbox["unread_messages"] or inbox["tasks_needing_input"]:
            return inbox
        await asyncio.sleep(2)

    return {"pending_tasks": [], "unread_messages": [], "tasks_needing_input": []}


@app.post("/api/v1/inbox/{agent_id}/ack")
async def acknowledge_inbox(agent_id: str, body: AcknowledgeRequest, request: Request):
    caller = await get_authenticated_agent(request)
    if caller.get("trust_tier", 3) > 1 and caller["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="Can only acknowledge own inbox")

    db: Database = request.app.state.db
    count = await db.acknowledge(agent_id, body.delivery_ids)
    return {"acknowledged": count}


# --- Artifacts ---

@app.post("/api/v1/tasks/{task_id}/artifacts")
async def create_artifact(task_id: str, body: ArtifactCreateRequest, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    check_read_permission(caller, task)

    parts = [p.model_dump() for p in body.parts] if body.parts else None
    artifact = await db.create_artifact(task_id, body.name, body.content,
                                        body.mime_type, parts)
    return artifact


@app.get("/api/v1/tasks/{task_id}/artifacts")
async def get_artifacts(task_id: str, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    check_read_permission(caller, task)

    artifacts = await db.get_artifacts(task_id)
    return {"artifacts": artifacts}


# --- Broadcast ---

@app.post("/api/v1/broadcast")
async def broadcast(body: BroadcastRequest, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    delivery_ids = await db.broadcast(caller["agent_id"], body.content, body.metadata)
    return {"delivery_ids": delivery_ids, "recipients": len(delivery_ids)}


# --- Audit ---

@app.get("/api/v1/audit")
async def get_audit(
    request: Request,
    since: str = Query(None, alias="from"),
    until: str = Query(None, alias="to"),
    agent: str = Query(None),
    limit: int = Query(100, le=500),
):
    caller = await get_authenticated_agent(request)
    if caller.get("trust_tier", 3) > 1:
        raise HTTPException(status_code=403, detail="Audit log requires Tier 1 access")

    db: Database = request.app.state.db
    entries = await db.get_audit_log(since=since, until=until, agent_id=agent, limit=limit)
    return {"entries": entries}


# --- Run ---

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("relay.server:app", host=HOST, port=PORT, reload=True)
