"""
Relay configuration.
"""

import os
from pathlib import Path

# Server
HOST = os.getenv("RELAY_HOST", "0.0.0.0")
PORT = int(os.getenv("RELAY_PORT", "8400"))

# Database
DB_PATH = os.getenv("RELAY_DB_PATH", str(Path(__file__).parent / "relay.db"))

# Auth
# If true, require Authorization: Bearer <token> on all API calls
AUTH_ENABLED = os.getenv("RELAY_AUTH_ENABLED", "true").lower() == "true"
