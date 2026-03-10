#!/usr/bin/env python3
"""
MCP Bridge for Agent Relay.

Exposes the relay as 10 MCP tools that Claude Code and Abby can call natively.
Communicates with the relay server via HTTP.

Usage:
    claude mcp add relay -- python3 /path/to/relay/mcp_bridge.py

Environment variables:
    RELAY_URL: Base URL of the relay server (default: http://localhost:8400)
    RELAY_API_KEY: Bearer token for authentication
    AGENT_ID: This agent's ID (default: claude-code)
"""

import json
import os
import sys
from typing import Any

import httpx

# MCP protocol uses JSON-RPC 2.0 over stdio
RELAY_URL = os.getenv("RELAY_URL", "http://localhost:8400")
AGENT_ID = os.getenv("AGENT_ID", "claude-code")


def _load_api_key() -> str:
    """Load API key from env var or file. File takes precedence."""
    key_file = os.getenv("RELAY_API_KEY_FILE", "")
    if key_file:
        path = os.path.expanduser(key_file)
        try:
            return open(path).read().strip()
        except OSError:
            pass
    return os.getenv("RELAY_API_KEY", "")


RELAY_API_KEY = _load_api_key()

TOOLS = [
    {
        "name": "relay_inbox",
        "description": "Check your relay inbox — pending tasks, unread messages, and tasks needing your input. Call at session start.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max items to return", "default": 20},
                "from_agent": {"type": "string", "description": "Filter by sender agent ID"},
                "since": {"type": "string", "description": "ISO datetime — only items after this"},
            },
        },
    },
    {
        "name": "relay_send_task",
        "description": "Create and send a task to another agent. Use for delegation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to_agent": {"type": "string", "description": "Target agent ID (e.g. 'clawdia', 'abby')"},
                "title": {"type": "string", "description": "Short task title"},
                "description": {"type": "string", "description": "Detailed task description with context"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
                "due_by": {"type": "string", "description": "ISO datetime deadline (optional)"},
                "metadata": {"type": "object", "description": "Extra context (stream, repo, etc.)"},
            },
            "required": ["to_agent", "title"],
        },
    },
    {
        "name": "relay_update_task",
        "description": "Update a task's status (accept, complete, reject, fail, set input_needed). Optionally include a message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to update"},
                "status": {"type": "string", "enum": ["accepted", "working", "completed", "rejected", "failed", "input_needed", "cancelled"]},
                "message": {"type": "string", "description": "Optional message explaining the update"},
            },
            "required": ["task_id", "status"],
        },
    },
    {
        "name": "relay_send_message",
        "description": "Add a message to a task thread. For follow-ups, questions, or updates within an existing task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to message on"},
                "content": {"type": "string", "description": "Message text"},
            },
            "required": ["task_id", "content"],
        },
    },
    {
        "name": "relay_get_task",
        "description": "Get full task details including message history and artifacts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to retrieve"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "relay_list_tasks",
        "description": "Query tasks with filters. See what's been delegated, completed, or in progress.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["submitted", "accepted", "working", "input_needed", "completed", "rejected", "failed", "cancelled"]},
                "to": {"type": "string", "description": "Filter by target agent"},
                "from_agent": {"type": "string", "description": "Filter by sender agent"},
                "since": {"type": "string", "description": "ISO datetime — only tasks created after this"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "relay_agents",
        "description": "List all registered agents with their capabilities and online/offline status.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "relay_broadcast",
        "description": "Send an FYI message to all agents (travel notices, status changes, system updates).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Broadcast message"},
                "metadata": {"type": "object", "description": "Optional metadata (type, valid_until, etc.)"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "relay_attach_artifact",
        "description": "Attach an output/result to a task (calendar event, email confirmation, code commit, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "name": {"type": "string", "description": "Artifact name (e.g. 'calendar_event', 'email_sent')"},
                "content": {"type": "string", "description": "Artifact content (text or JSON string)"},
                "mime_type": {"type": "string", "description": "MIME type (default: text/plain)"},
            },
            "required": ["task_id", "name", "content"],
        },
    },
    {
        "name": "relay_wait_inbox",
        "description": "Long-poll for new inbox items. Blocks until something arrives or timeout. Use when waiting for a specific response instead of polling.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout": {"type": "integer", "description": "Max seconds to wait (default 30, max 60)", "default": 30},
                "from_agent": {"type": "string", "description": "Filter by sender agent ID"},
                "since": {"type": "string", "description": "ISO datetime — only items after this"},
            },
        },
    },
    {
        "name": "relay_heartbeat",
        "description": "Update your online/offline/busy status. Called automatically at session start and end.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["online", "offline", "busy"], "default": "online"},
            },
        },
    },
]


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if RELAY_API_KEY:
        h["Authorization"] = f"Bearer {RELAY_API_KEY}"
    else:
        h["X-Agent-ID"] = AGENT_ID
    return h


def _api(method: str, path: str, body: dict | None = None, timeout: int = 10) -> dict:
    """Synchronous HTTP call to relay API."""
    url = f"{RELAY_URL}/api/v1{path}"
    try:
        with httpx.Client(timeout=timeout) as client:
            if method == "GET":
                resp = client.get(url, headers=_headers(), params=body)
            elif method == "POST":
                resp = client.post(url, headers=_headers(), json=body or {})
            elif method == "PATCH":
                resp = client.patch(url, headers=_headers(), json=body or {})
            else:
                return {"error": f"Unknown method: {method}"}

            if resp.status_code >= 400:
                return {"error": f"HTTP {resp.status_code}: {resp.text}"}
            return resp.json()
    except httpx.ConnectError:
        return {"error": f"Cannot connect to relay at {RELAY_URL}. Is it running?"}
    except Exception as e:
        return {"error": str(e)}


def handle_tool_call(name: str, args: dict) -> Any:
    """Route MCP tool call to relay API."""

    if name == "relay_inbox":
        params = {"limit": args.get("limit", 20)}
        if args.get("from_agent"):
            params["from"] = args["from_agent"]
        if args.get("since"):
            params["since"] = args["since"]
        return _api("GET", f"/inbox/{AGENT_ID}", params)

    elif name == "relay_send_task":
        return _api("POST", "/tasks", {
            "to_agent": args["to_agent"],
            "title": args["title"],
            "description": args.get("description"),
            "priority": args.get("priority", "normal"),
            "due_by": args.get("due_by"),
            "metadata": args.get("metadata"),
        })

    elif name == "relay_update_task":
        return _api("PATCH", f"/tasks/{args['task_id']}", {
            "status": args["status"],
            "message": args.get("message"),
        })

    elif name == "relay_send_message":
        return _api("POST", f"/tasks/{args['task_id']}/messages", {
            "content": args["content"],
        })

    elif name == "relay_get_task":
        return _api("GET", f"/tasks/{args['task_id']}")

    elif name == "relay_list_tasks":
        params = {}
        if args.get("status"):
            params["status"] = args["status"]
        if args.get("to"):
            params["to"] = args["to"]
        if args.get("from_agent"):
            params["from"] = args["from_agent"]
        if args.get("since"):
            params["since"] = args["since"]
        params["limit"] = args.get("limit", 20)
        return _api("GET", "/tasks", params)

    elif name == "relay_agents":
        return _api("GET", "/agents")

    elif name == "relay_broadcast":
        return _api("POST", "/broadcast", {
            "content": args["content"],
            "metadata": args.get("metadata"),
        })

    elif name == "relay_attach_artifact":
        return _api("POST", f"/tasks/{args['task_id']}/artifacts", {
            "name": args["name"],
            "content": args["content"],
            "mime_type": args.get("mime_type", "text/plain"),
        })

    elif name == "relay_wait_inbox":
        wait_timeout = args.get("timeout", 30)
        params = {"timeout": wait_timeout}
        if args.get("from_agent"):
            params["from"] = args["from_agent"]
        if args.get("since"):
            params["since"] = args["since"]
        return _api("GET", f"/inbox/{AGENT_ID}/wait", params, timeout=wait_timeout + 5)

    elif name == "relay_heartbeat":
        status = args.get("status", "online")
        return _api("POST", f"/agents/{AGENT_ID}/heartbeat", {"status": status})

    return {"error": f"Unknown tool: {name}"}


def main():
    """MCP server main loop — JSON-RPC 2.0 over stdio."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _respond_error(None, -32700, "Parse error")
            continue

        req_id = request.get("id")
        method = request.get("method", "")

        if method == "initialize":
            _respond(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "agent-relay",
                    "version": "0.1.0",
                },
            })

        elif method == "notifications/initialized":
            pass  # No response needed for notifications

        elif method == "tools/list":
            _respond(req_id, {"tools": TOOLS})

        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            result = handle_tool_call(tool_name, tool_args)
            _respond(req_id, {
                "content": [
                    {"type": "text", "text": json.dumps(result, indent=2)}
                ],
            })

        else:
            _respond_error(req_id, -32601, f"Method not found: {method}")


def _respond(req_id, result):
    msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _respond_error(req_id, code, message):
    msg = json.dumps({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    })
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
