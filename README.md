# Agent Relay

A lightweight message relay for AI agents that aren't always on.

## The Problem

Google's [A2A protocol](https://github.com/google/A2A) assumes agents are permanently reachable HTTP services. But many real-world agents — CLI tools like Claude Code, local LLM runners, cron-triggered workers, laptop-bound assistants — only exist when a terminal session is open or a schedule fires.

Agent Relay bridges that gap. It provides persistent message queuing, agent discovery, and structured task handoffs for agents that come and go. Messages wait. Tasks track lifecycle. The relay is the only thing that needs to stay on.

## Key Features

- **Offline-first queuing** — messages persist until the target agent polls or reconnects
- **MCP-native** — ships with an MCP bridge so Claude Code agents need zero custom client code
- **A2A-aligned data model** — Tasks, Messages, Artifacts follow A2A conventions for future migration
- **Trust tiers** — agents have scoped permissions (who they can read from, send to, what tools they access)
- **SQLite storage** — no Postgres, no Redis, no Docker Compose. One file, crash-safe WAL mode
- **Tailscale-friendly** — designed for private agent meshes on Tailscale networks
- **Full audit trail** — every message, delivery, state change logged permanently

## Quick Start

### 1. Install

```bash
git clone https://github.com/hazzap123/agent-relay.git
cd agent-relay
python3 -m venv venv && source venv/bin/activate
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — set API keys for your agents
```

### 3. Run

```bash
uvicorn relay.server:app --host 0.0.0.0 --port 8400
```

### 4. Register with Claude Code (MCP)

```bash
claude mcp add relay -- python3 /path/to/agent-relay/relay/mcp_bridge.py
```

Set environment variables for the bridge:
```bash
RELAY_URL=http://your-relay-host:8400
RELAY_API_KEY=your-agent-api-key
AGENT_ID=claude-code
```

## API

14 REST endpoints under `/api/v1`:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/agents` | List all registered agents |
| POST | `/api/v1/agents` | Register a new agent |
| GET | `/api/v1/agents/{id}` | Get agent details |
| POST | `/api/v1/agents/{id}/heartbeat` | Update online/offline status |
| POST | `/api/v1/tasks` | Create and send a task |
| GET | `/api/v1/tasks` | Query tasks with filters |
| GET | `/api/v1/tasks/{id}` | Get task details |
| PUT | `/api/v1/tasks/{id}` | Update task status |
| POST | `/api/v1/messages` | Send a message |
| GET | `/api/v1/inbox` | Get pending messages for an agent |
| POST | `/api/v1/broadcast` | Send to all agents |
| POST | `/api/v1/tasks/{id}/artifacts` | Attach an artifact to a task |
| GET | `/api/v1/health` | Health check |
| GET | `/api/v1/stats` | System statistics |

## MCP Tools

The MCP bridge exposes 10 tools that Claude Code agents can call natively:

- `relay_inbox` — check for pending messages
- `relay_send_task` — delegate a task to another agent
- `relay_update_task` — update task status (accepted, working, completed, etc.)
- `relay_send_message` — send a free-form message
- `relay_get_task` — get task details
- `relay_list_tasks` — query tasks with filters
- `relay_agents` — list registered agents and their status
- `relay_broadcast` — send a message to all agents
- `relay_attach_artifact` — attach output to a task
- `relay_heartbeat` — update your online/offline status

## Agent Configuration

Copy `agents.example.json` and define your agents:

```json
[
  {
    "agent_id": "claude-code",
    "name": "Claude Code",
    "capabilities": ["code", "analysis", "memory"],
    "trust_tier": 1,
    "contact": { "method": "poll" }
  },
  {
    "agent_id": "assistant",
    "name": "Assistant",
    "capabilities": ["email", "scheduling"],
    "trust_tier": 2,
    "contact": { "method": "webhook", "webhook_url": "http://..." }
  }
]
```

Trust tiers control permissions:
- **Tier 1** — full access, can read from and send to all agents
- **Tier 2** — scoped access, restricted read/send permissions
- **Tier 3** — minimal access, heavily sandboxed

## Deployment

A systemd service file and deploy script are included:

```bash
DEPLOY_HOST=root@your-server RELAY_HOST=your-server-ip bash deploy.sh
```

See `agent-relay.service` for the systemd unit.

## Testing

```bash
python -m pytest tests/ -v
```

## A2A Alignment

Agent Relay borrows A2A's data model and lifecycle states but doesn't implement the full A2A JSON-RPC 2.0 spec. This is deliberate — A2A assumes always-on HTTP services, which doesn't match CLI agents or intermittent workers.

| A2A Concept | Agent Relay | Status |
|-------------|-------------|--------|
| AgentCard | Agent registry with capabilities | Implemented |
| Task lifecycle | submitted → accepted → working → completed/failed | Implemented |
| Message/Part | Structured messages with metadata | Implemented |
| Artifact | Task attachments | Implemented |
| JSON-RPC 2.0 | REST + MCP bridge | Different transport, same semantics |
| Streaming (SSE) | Polling + webhooks | Planned |

The data model is designed so that migrating to full A2A compliance is additive, not a rewrite.

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Claude Code │     │   Clawdia    │     │    Abby      │
│  (CLI, Mac)  │     │  (daemon)    │     │  (CLI, Mac)  │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │ MCP                │ HTTP/poll          │ MCP
       │                    │                    │
       ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────┐
│                     Agent Relay                         │
│  FastAPI · SQLite · Bearer auth · Trust tiers           │
│  Tailscale network — private, zero-config               │
└─────────────────────────────────────────────────────────┘
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
