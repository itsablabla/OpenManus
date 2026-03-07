"""
Pydantic input models for the Manus API MCP tools.

All inputs are validated before hitting the Manus API — catches bad args
early with clear messages rather than letting the API return a 400.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class TaskMode(str, Enum):
    CHAT = "chat"
    ADAPTIVE = "adaptive"
    AGENT = "agent"


class AgentProfile(str, Enum):
    SPEED = "speed"      # ~50-100 credits, Manus standard
    QUALITY = "quality"  # ~150-300 credits, Manus 1.6 Max


class CreateTaskInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    prompt: str = Field(
        ...,
        min_length=10,
        max_length=10000,
        description=(
            "Natural language task for Manus to execute autonomously. "
            "Be specific. Example: 'Research the top 5 rural ISP competitors to "
            "Nomad Internet and produce a comparison table with pricing, coverage, and reviews.'"
        ),
    )
    task_mode: TaskMode = Field(
        default=TaskMode.AGENT,
        description="agent=full autonomous execution (default), adaptive=Manus decides, chat=conversational only",
    )
    agent_profile: AgentProfile = Field(
        default=AgentProfile.SPEED,
        description="speed=faster/cheaper (~100 credits), quality=Manus 1.6 Max (~200 credits)",
    )
    file_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional list of file_ids from manus_upload_file to attach as context",
    )
    use_gmail_connector: bool = Field(
        default=False,
        description="Grant Manus access to Gmail (requires MANUS_GMAIL_CONNECTOR_ID env var on Railway)",
    )
    use_notion_connector: bool = Field(
        default=False,
        description="Grant Manus access to Notion (requires MANUS_NOTION_CONNECTOR_ID env var on Railway)",
    )
    use_gcal_connector: bool = Field(
        default=False,
        description="Grant Manus access to Google Calendar (requires MANUS_GCAL_CONNECTOR_ID env var on Railway)",
    )


class GetTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(..., min_length=1, description="Task ID returned by manus_create_task")


class ListTasksInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=1, le=100, description="Number of tasks to return")
    offset: int = Field(default=0, ge=0, description="Pagination offset")
    status: Optional[str] = Field(
        default=None,
        description="Filter by status: pending | running | completed | failed",
    )


class CreateWebhookInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(
        ...,
        min_length=10,
        description="Webhook URL to receive task completion events. Use your n8n webhook trigger URL.",
    )
    events: List[str] = Field(
        default=["task.completed", "task.failed"],
        description="Events to subscribe to. Options: task.completed, task.failed, task.updated",
    )
