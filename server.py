"""
Agent Relay — FastAPI server.

A2A-compatible message relay for CLI-based AI agents.
Run: python -m relay.server
"""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .auth import (
    check_read_permission,
    check_send_permission,
    check_task_update_permission,
    get_authenticated_agent,
)
from .config import AUTH_ENABLED, DB_PATH, HOST, PORT
from .db import Database
from .models import (
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
    agent, api_key = await db.register_agent(
        agent_id=body.agent_id,
        name=body.name,
        description=body.description,
        version=body.version,
        capabilities=body.capabilities,
        contact_method=body.contact.method.value,
        webhook_url=body.contact.webhook_url,
        trust_tier=body.trust_tier,
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
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


@app.post("/api/v1/agents/{agent_id}/heartbeat")
async def heartbeat(agent_id: str, body: HeartbeatRequest, request: Request):
    db: Database = request.app.state.db
    # Allow heartbeat with just X-Agent-ID (no full auth required for heartbeat)
    success = await db.heartbeat(agent_id, body.status.value)
    if not success:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return {"status": "ok"}


@app.delete("/api/v1/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request):
    caller = await get_authenticated_agent(request)
    if caller.get("trust_tier", 3) > 1 and caller["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="Only Tier 1 agents can delete other agents")
    db: Database = request.app.state.db
    success = await db.delete_agent(agent_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return {"status": "deleted"}


# --- Tasks ---

@app.post("/api/v1/tasks")
async def create_task(body: TaskCreateRequest, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    # Check target agent exists
    target = await db.get_agent(body.to_agent)
    if not target:
        raise HTTPException(status_code=404, detail=f"Target agent '{body.to_agent}' not found")

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
    return task


@app.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db
    task = await db.get_task_with_messages(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    check_read_permission(caller, task)
    return task


@app.patch("/api/v1/tasks/{task_id}")
async def update_task(task_id: str, body: TaskUpdateRequest, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    check_task_update_permission(caller, task, body.status.value)

    updated = await db.update_task_status(task_id, body.status.value, caller["agent_id"])

    # If a message was included with the status update, create it
    if body.message:
        await db.create_message(task_id, caller["agent_id"], body.message)

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
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    check_read_permission(caller, task)

    parts = [p.model_dump() for p in body.parts] if body.parts else None
    message = await db.create_message(task_id, caller["agent_id"], body.content, parts)
    return message


@app.get("/api/v1/tasks/{task_id}/messages")
async def get_messages(task_id: str, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    check_read_permission(caller, task)

    messages = await db.get_messages(task_id)
    return {"messages": messages}


# --- Inbox ---

@app.get("/api/v1/inbox/{agent_id}")
async def get_inbox(agent_id: str, request: Request):
    caller = await get_authenticated_agent(request)

    # Non-tier-1 can only check own inbox
    if caller.get("trust_tier", 3) > 1 and caller["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="Can only check own inbox")

    db: Database = request.app.state.db
    inbox = await db.get_inbox(agent_id)
    return inbox


@app.post("/api/v1/inbox/{agent_id}/ack")
async def acknowledge_inbox(agent_id: str, body: dict, request: Request):
    caller = await get_authenticated_agent(request)
    if caller.get("trust_tier", 3) > 1 and caller["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="Can only acknowledge own inbox")

    db: Database = request.app.state.db
    delivery_ids = body.get("delivery_ids", [])
    count = await db.acknowledge(agent_id, delivery_ids)
    return {"acknowledged": count}


# --- Artifacts ---

@app.post("/api/v1/tasks/{task_id}/artifacts")
async def create_artifact(task_id: str, body: ArtifactCreateRequest, request: Request):
    caller = await get_authenticated_agent(request)
    db: Database = request.app.state.db

    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
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
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
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
