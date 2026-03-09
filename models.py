"""
Pydantic models for Agent Relay — A2A-aligned data model.

Core entities: AgentCard, Task, Message, Artifact, Delivery, AuditEntry.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


# --- Enums ---

class TaskStatus(str, Enum):
    submitted = "submitted"
    accepted = "accepted"
    working = "working"
    input_needed = "input_needed"
    completed = "completed"
    rejected = "rejected"
    failed = "failed"
    cancelled = "cancelled"


class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class ContactMethod(str, Enum):
    poll = "poll"
    webhook = "webhook"


class AgentStatus(str, Enum):
    online = "online"
    offline = "offline"
    busy = "busy"


class DeliveryStatus(str, Enum):
    pending = "pending"
    delivered = "delivered"
    acknowledged = "acknowledged"
    failed = "failed"


# --- Part model (A2A-aligned) ---

class Part(BaseModel):
    type: str  # "text", "data", "file"
    content: Optional[str] = None
    mime_type: Optional[str] = None
    data: Optional[dict] = None
    name: Optional[str] = None
    uri: Optional[str] = None


# --- Agent Card ---

class AgentContact(BaseModel):
    method: ContactMethod = ContactMethod.poll
    webhook_url: Optional[str] = None

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v):
        if v is None:
            return v
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("webhook_url must use http or https scheme")
        host = parsed.hostname or ""
        if host == "169.254.169.254":
            raise ValueError("webhook_url cannot target metadata service")
        return v


class AgentPermissions(BaseModel):
    can_read_from: list[str] = Field(default_factory=lambda: ["*"])
    can_send_to: list[str] = Field(default_factory=lambda: ["*"])
    can_access_tools: list[str] = Field(default_factory=list)


class AgentRegisterRequest(BaseModel):
    agent_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    version: str = "1.0.0"
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    contact: AgentContact = Field(default_factory=AgentContact)
    trust_tier: int = Field(3, ge=1, le=3)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    metadata: Optional[dict] = None
    api_key: Optional[str] = None  # If not provided, one is generated


class AgentCard(BaseModel):
    agent_id: str
    name: str
    description: Optional[str] = None
    version: str = "1.0.0"
    capabilities: list[str] = Field(default_factory=list)
    contact: AgentContact = Field(default_factory=AgentContact)
    trust_tier: int = 3
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    status: AgentStatus = AgentStatus.offline
    last_seen: Optional[str] = None
    metadata: Optional[dict] = None
    registered_at: str = ""
    updated_at: str = ""


# --- Task ---

class TaskCreateRequest(BaseModel):
    to_agent: str = Field(..., max_length=64)
    title: str = Field(..., max_length=500)
    description: Optional[str] = Field(None, max_length=10000)
    priority: Priority = Priority.normal
    due_by: Optional[str] = None
    metadata: Optional[dict] = None

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v):
        if v is None:
            return v
        if len(v) > 20:
            raise ValueError("metadata limited to 20 keys")
        for key, val in v.items():
            if not isinstance(key, str) or len(key) > 100:
                raise ValueError("metadata keys must be strings under 100 chars")
            if isinstance(val, str) and len(val) > 2000:
                raise ValueError("metadata string values must be under 2000 chars")
        return v


class TaskUpdateRequest(BaseModel):
    status: TaskStatus
    message: Optional[str] = None  # Convenience: auto-creates a message


class Task(BaseModel):
    task_id: str
    title: str
    description: Optional[str] = None
    from_agent: str
    to_agent: str
    status: TaskStatus = TaskStatus.submitted
    priority: Priority = Priority.normal
    due_by: Optional[str] = None
    metadata: Optional[dict] = None
    created_at: str = ""
    updated_at: str = ""


class TaskWithMessages(Task):
    messages: list["Message"] = Field(default_factory=list)
    artifacts: list["Artifact"] = Field(default_factory=list)


# --- Message ---

class MessageCreateRequest(BaseModel):
    content: str = Field(..., max_length=50000)
    parts: Optional[list[Part]] = None  # Or provide structured parts


class Message(BaseModel):
    message_id: str
    task_id: str
    from_agent: str
    role: str = "agent"
    parts: list[Part] = Field(default_factory=list)
    created_at: str = ""


# --- Artifact ---

class ArtifactCreateRequest(BaseModel):
    name: str = Field(..., max_length=200)
    content: Optional[str] = Field(None, max_length=100000)
    mime_type: Optional[str] = Field(None, max_length=100)
    parts: Optional[list[Part]] = None


class Artifact(BaseModel):
    artifact_id: str
    task_id: str
    name: str
    parts: list[Part] = Field(default_factory=list)
    created_at: str = ""


# --- Broadcast ---

class BroadcastRequest(BaseModel):
    content: str = Field(..., max_length=50000)
    metadata: Optional[dict] = None


# --- Inbox ---

class InboxResponse(BaseModel):
    pending_tasks: list[Task] = Field(default_factory=list)
    unread_messages: list[dict] = Field(default_factory=list)
    tasks_needing_input: list[Task] = Field(default_factory=list)


# --- Heartbeat ---

class AcknowledgeRequest(BaseModel):
    delivery_ids: list[str] = Field(..., max_length=100)


class HeartbeatRequest(BaseModel):
    status: AgentStatus = AgentStatus.online


# --- Health ---

class HealthResponse(BaseModel):
    status: str = "ok"
    agents: int = 0
    pending_tasks: int = 0
    uptime_seconds: float = 0


# --- Audit ---

class AuditEntry(BaseModel):
    log_id: int
    event_type: str
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    detail: Optional[dict] = None
    created_at: str = ""
