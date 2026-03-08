"""
Authentication and trust tier enforcement for Agent Relay.

Two layers:
1. Bearer token auth — validates agent identity
2. Trust tier permissions — controls what agents can do
"""

from fastapi import HTTPException, Request

from .db import Database


async def get_authenticated_agent(request: Request) -> dict:
    """Extract and validate agent identity from request headers.

    Supports two auth modes:
    - Bearer token: Authorization: Bearer <api_key> (preferred)
    - Legacy header: X-Agent-ID: <agent_id> (fallback when auth disabled)
    """
    db: Database = request.app.state.db
    auth_enabled = request.app.state.auth_enabled

    authorization = request.headers.get("authorization", "")
    x_agent_id = request.headers.get("x-agent-id", "")

    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
        # Find agent by trying each registered agent's key
        agents = await db.list_agents()
        for agent in agents:
            if await db.verify_api_key(agent["agent_id"], api_key):
                return agent
        raise HTTPException(status_code=401, detail="Invalid API key")

    if x_agent_id:
        agent = await db.get_agent(x_agent_id)
        if not agent:
            raise HTTPException(status_code=401, detail=f"Unknown agent: {x_agent_id}")
        if auth_enabled:
            raise HTTPException(
                status_code=401,
                detail="Bearer token required when auth is enabled. "
                       "Use Authorization: Bearer <api_key>")
        return agent

    raise HTTPException(status_code=401, detail="Missing authentication. "
                        "Provide Authorization: Bearer <key> or X-Agent-ID header.")


def check_send_permission(from_agent: dict, to_agent_id: str):
    """Check if from_agent is allowed to send to to_agent_id."""
    perms = from_agent.get("permissions", {})
    allowed = perms.get("can_send_to", ["*"])
    if "*" in allowed or to_agent_id in allowed:
        return
    raise HTTPException(
        status_code=403,
        detail=f"Agent '{from_agent['agent_id']}' not permitted to send to '{to_agent_id}'")


def check_read_permission(agent: dict, task: dict):
    """Check if agent can read this task."""
    agent_id = agent["agent_id"]
    tier = agent.get("trust_tier", 3)

    # Tier 1 can read everything
    if tier == 1:
        return

    # Others can only read tasks they're party to
    if task["from_agent"] == agent_id or task["to_agent"] == agent_id:
        return

    raise HTTPException(
        status_code=403,
        detail=f"Agent '{agent_id}' not permitted to read task '{task['task_id']}'")


def check_task_update_permission(agent: dict, task: dict, new_status: str):
    """Check if agent can update this task's status."""
    agent_id = agent["agent_id"]

    # Receiver can accept, complete, reject, fail, set input_needed
    if task["to_agent"] == agent_id:
        return

    # Sender can cancel
    if task["from_agent"] == agent_id and new_status == "cancelled":
        return

    # Tier 1 can do anything (admin)
    if agent.get("trust_tier", 3) == 1:
        return

    raise HTTPException(
        status_code=403,
        detail=f"Agent '{agent_id}' not permitted to update task '{task['task_id']}'")
