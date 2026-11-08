from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


TaskStatus = Literal["pending", "ready", "running", "done", "failed", "skipped"]
RunStatus = Literal["planning", "running", "succeeded", "failed", "cancelled"]


class Budget(BaseModel):
    max_steps: int = Field(default=60, ge=1, le=1000)
    max_tokens: int = Field(default=2_000_000, ge=1_000)
    max_usd: float = Field(default=1.0, ge=0)
    deadline_sec: int = Field(default=3600, ge=10, le=86_400)


class CreateRunRequest(BaseModel):
    goal: str = Field(min_length=2, max_length=4000)
    budget: Budget | None = None
    cluster_id: str | None = Field(default=None, max_length=80)
    cluster_mode: bool = True
    device_id: str | None = Field(default=None, max_length=80)
    workspace: str | None = Field(default=None, max_length=1000)


class DeviceInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ClusterInput(BaseModel):
    id: str | None = Field(default=None, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    provider_ids: list[str] = Field(default_factory=list, max_length=50)
    planner_id: str | None = Field(default=None, max_length=80)
    is_default: bool = False


class ResumeRunInput(BaseModel):
    task_id: str | None = Field(default=None, max_length=80)


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=12000)


class BranchInput(BaseModel):
    message_id: str = Field(min_length=1, max_length=80)


class ProviderInput(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=60)
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=120)
    api_key: str = Field(default="", max_length=500)
    api_style: Literal["openai_chat", "openai_responses", "anthropic"] = "openai_chat"
    auth_style: Literal["bearer", "x-api-key", "api-key", "none"] = "bearer"
    extra_headers: str = Field(default="", max_length=4000)
    proxy: str = Field(default="", max_length=500)
    scores: dict[str, float] = Field(default_factory=dict)
    reasoning_effort: Literal["", "low", "medium", "high"] = ""
    timeout: int = Field(default=0, ge=0, le=3600)
    stream: bool = True
    enabled: bool = True
    is_default: bool = False
    group_id: str | None = Field(default=None, max_length=80)
    copy_from: str | None = Field(default=None, max_length=80)


class ProviderTestInput(BaseModel):
    id: str | None = None
    copy_from: str | None = Field(default=None, max_length=80)
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=120)
    api_key: str = Field(default="", max_length=500)
    api_style: Literal["openai_chat", "openai_responses", "anthropic"] | None = None
    auth_style: Literal["bearer", "x-api-key", "api-key", "none"] | None = None
    extra_headers: str = Field(default="", max_length=4000)
    proxy: str = Field(default="", max_length=500)
    timeout: int | None = Field(default=None, ge=0, le=3600)


class ProviderEnabledInput(BaseModel):
    enabled: bool = True


class ProviderScoreInput(BaseModel):
    judge_id: str | None = Field(default=None, max_length=80)


class BatchScoreInput(BaseModel):
    judge_id: str | None = Field(default=None, max_length=80)
    provider_ids: list[str] | None = Field(default=None, max_length=100)


class RuntimeSettingsInput(BaseModel):
    workspace_dir: str | None = Field(default=None, max_length=1000)
    shell_enabled: bool | None = None
    command_approval: bool | None = None
    approval_timeout: int | None = Field(default=None, ge=30, le=3600)


class ApprovalInput(BaseModel):
    approved: bool


class MCPServerInput(BaseModel):
    id: str | None = Field(default=None, max_length=80)
    name: str = Field(default="", max_length=80)
    transport: Literal["stdio", "sse", "streamable_http"] = "stdio"
    command: str = Field(default="", max_length=1000)
    args: list[str] = Field(default_factory=list, max_length=100)
    cwd: str = Field(default="", max_length=1000)
    env: dict[str, str] | None = None
    url: str = Field(default="", max_length=2000)
    headers: dict[str, str] | None = None
    enabled: bool = True


class PlannedTask(BaseModel):
    title: str = Field(max_length=120)
    instruction: str = Field(max_length=4000)
    capability: str = Field(max_length=40)
    depends_on: list[int] = Field(default_factory=list)


class PlanOutput(BaseModel):
    tasks: list[PlannedTask] = Field(min_length=1, max_length=12)


@dataclass
class AgentSpec:
    id: str
    name: str
    capabilities: list[str]
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    max_steps: int = 6
    description: str = ""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class TaskState:
    id: str
    run_id: str
    order: int
    title: str
    instruction: str
    capability: str
    round: int = 1
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = "pending"
    assignee: str | None = None
    model_id: str | None = None
    model_name: str | None = None
    result: str = ""
    error: str = ""
    attempts: int = 0
    steps: int = 0
    tokens: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    # Durable step checkpoint: messages, tool observations and next step.
    # Kept in SQLite so a restarted process can continue an in-flight task.
    checkpoint: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "order": self.order,
            "round": self.round,
            "title": self.title,
            "instruction": self.instruction,
            "capability": self.capability,
            "depends_on": list(self.depends_on),
            "status": self.status,
            "assignee": self.assignee,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "steps": self.steps,
            "tokens": self.tokens,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class RunState:
    id: str
    goal: str
    status: RunStatus
    budget: Budget
    tasks: list[TaskState] = field(default_factory=list)
    result: str = ""
    error: str = ""
    steps: int = 0
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    created_at: float = field(default_factory=now)
    deadline_at: float = 0.0
    cancelled: bool = False
    cluster: dict[str, Any] = field(default_factory=dict)
    # Set while the process is shutting down; cancellation must not turn a
    # resumable run into a terminal one.
    shutting_down: bool = False
