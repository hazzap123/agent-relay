#!/bin/bash
# Agent Relay — Deploy to a remote Linux server via SSH
# Run from your machine: bash relay/deploy.sh
#
# Configure via environment variables:
#   DEPLOY_HOST  — SSH target (e.g. root@192.168.1.50)
#   RELAY_HOST   — IP/hostname for curl health checks (e.g. 192.168.1.50)
#   RELAY_PORT   — Port (default: 8400)
#   RELAY_DIR    — Install path on server (default: /opt/agent-relay)
#   AGENTS_FILE  — JSON file defining agents to register (default: relay/agents.json)
#
# What this does:
# 1. Syncs relay code to the server
# 2. Creates a Python venv and installs dependencies
# 3. Sets up a systemd service
# 4. Starts the relay
# 5. Registers agents from agents.json
# 6. Saves API keys to relay/.env (gitignored)
# 7. Verifies with health check

set -euo pipefail

DEPLOY_HOST="${DEPLOY_HOST:?Set DEPLOY_HOST (e.g. root@192.168.1.50)}"
RELAY_DIR="${RELAY_DIR:-/opt/agent-relay}"
RELAY_PORT="${RELAY_PORT:-8400}"
RELAY_HOST="${RELAY_HOST:?Set RELAY_HOST (e.g. 192.168.1.50)}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
AGENTS_FILE="${AGENTS_FILE:-${SCRIPT_DIR}/agents.json}"

echo "=== Agent Relay Deployment ==="
echo "  Target: ${DEPLOY_HOST}:${RELAY_PORT}"
echo ""

# --- Step 1: Sync code ---
echo "[1/7] Syncing relay code..."
ssh "$DEPLOY_HOST" "mkdir -p ${RELAY_DIR}"
rsync -avz --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    --exclude='relay.db' \
    --exclude='.env' \
    --exclude='tests/' \
    --exclude='agents.json' \
    "${SCRIPT_DIR}/" "${DEPLOY_HOST}:${RELAY_DIR}/relay/"

# Copy pyproject.toml to parent for module resolution
ssh "$DEPLOY_HOST" "cat > ${RELAY_DIR}/pyproject.toml" <<'TOML'
[project]
name = "agent-relay-runner"
version = "0.1.0"
TOML

echo "  Done."

# --- Step 2: Set up venv and install dependencies ---
echo "[2/7] Setting up Python venv and installing dependencies..."
ssh "$DEPLOY_HOST" "apt-get install -y python3-venv > /dev/null 2>&1 || true"
ssh "$DEPLOY_HOST" "python3 -m venv ${RELAY_DIR}/venv"
ssh "$DEPLOY_HOST" "${RELAY_DIR}/venv/bin/pip install --upgrade pip > /dev/null 2>&1"
ssh "$DEPLOY_HOST" "${RELAY_DIR}/venv/bin/pip install fastapi uvicorn aiosqlite httpx pydantic 2>&1 | tail -1"
echo "  Done."

# --- Step 3: Set up systemd service ---
echo "[3/7] Setting up systemd service..."
ssh "$DEPLOY_HOST" "cat > /etc/systemd/system/agent-relay.service" <<EOF
[Unit]
Description=Agent Relay — A2A message relay for CLI agents
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${RELAY_DIR}
ExecStart=${RELAY_DIR}/venv/bin/uvicorn relay.server:app --host 0.0.0.0 --port ${RELAY_PORT}
Restart=always
RestartSec=5
Environment=RELAY_DB_PATH=${RELAY_DIR}/relay.db
Environment=RELAY_AUTH_ENABLED=true

[Install]
WantedBy=multi-user.target
EOF

ssh "$DEPLOY_HOST" "systemctl daemon-reload"
echo "  Done."

# --- Step 4: Start/restart the relay ---
echo "[4/7] Starting relay service..."
ssh "$DEPLOY_HOST" "systemctl enable agent-relay && systemctl restart agent-relay"
sleep 2

# Check it's running
if ssh "$DEPLOY_HOST" "systemctl is-active agent-relay" | grep -q "active"; then
    echo "  Relay is running."
else
    echo "  ERROR: Relay failed to start. Checking logs..."
    ssh "$DEPLOY_HOST" "journalctl -u agent-relay -n 20 --no-pager"
    exit 1
fi

# --- Step 5: Health check ---
echo "[5/7] Health check..."
HEALTH=$(curl -s "http://${RELAY_HOST}:${RELAY_PORT}/health")
echo "  ${HEALTH}"

# --- Step 6: Register agents ---
echo "[6/7] Registering agents..."

if [ ! -f "$AGENTS_FILE" ]; then
    echo "  No agents.json found at ${AGENTS_FILE} — skipping registration."
    echo "  Create one from agents.example.json and re-run, or register manually via the API."
else
    register_agent() {
        local agent_id="$1"
        local json="$2"
        local resp
        resp=$(curl -s -X POST "http://${RELAY_HOST}:${RELAY_PORT}/api/v1/agents/register" \
            -H "Content-Type: application/json" \
            -d "$json")
        local key
        key=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])" 2>/dev/null)
        if [ -n "$key" ]; then
            echo "  Registered ${agent_id} (key: ${key:0:8}...)"
            local var_name
            var_name=$(echo "${agent_id}" | tr '[:lower:]-' '[:upper:]_')
            echo "${var_name}_API_KEY=${key}"
        else
            echo "  WARNING: Failed to register ${agent_id}: ${resp}"
        fi
    }

    # Collect keys
    {
        echo "# Agent Relay API Keys — generated $(date -Iseconds)"
        echo "RELAY_URL=http://${RELAY_HOST}:${RELAY_PORT}"
        echo ""

        # Read agents from JSON array
        AGENT_COUNT=$(python3 -c "import json; print(len(json.load(open('${AGENTS_FILE}'))))")
        for i in $(seq 0 $((AGENT_COUNT - 1))); do
            AGENT_ID=$(python3 -c "import json; print(json.load(open('${AGENTS_FILE}'))[$i]['agent_id'])")
            AGENT_JSON=$(python3 -c "import json; print(json.dumps(json.load(open('${AGENTS_FILE}'))[$i]))")
            register_agent "$AGENT_ID" "$AGENT_JSON"
        done
    } > "$ENV_FILE"

    echo "  API keys saved to relay/.env"
fi

# --- Step 7: Verify ---
echo "[7/7] Final verification..."

if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"

    # Find first API key in env file for verification
    FIRST_KEY=$(grep '_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2)
    if [ -n "$FIRST_KEY" ]; then
        AGENTS=$(curl -s -H "Authorization: Bearer ${FIRST_KEY}" \
            "http://${RELAY_HOST}:${RELAY_PORT}/api/v1/agents" | \
            python3 -c "import sys,json; agents=json.load(sys.stdin)['agents']; print(f'{len(agents)} agents registered: {[a[\"agent_id\"] for a in agents]}')")
        echo "  ${AGENTS}"
    fi
fi

HEALTH=$(curl -s "http://${RELAY_HOST}:${RELAY_PORT}/health")
echo "  Health: ${HEALTH}"

echo ""
echo "=== Deployment complete ==="
echo ""
echo "Next steps:"
echo "  1. Add MCP server to Claude Code:"
echo "     claude mcp add relay -e RELAY_URL=http://${RELAY_HOST}:${RELAY_PORT} -e RELAY_API_KEY=<key> -e AGENT_ID=<your-agent-id> -- python3 \$(pwd)/relay/mcp_bridge.py"
echo ""
echo "  2. Test from Claude Code:"
echo "     Use relay_inbox to check inbox"
echo "     Use relay_agents to list agents"
echo ""
