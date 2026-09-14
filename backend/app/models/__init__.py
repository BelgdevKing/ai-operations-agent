"""SQLAlchemy models.

Every model module must be imported here. Alembic autogenerate compares the
database against ``Base.metadata``, and a model that is never imported is not
in that metadata - so its table would be silently dropped from migrations.

Tenant ownership: every table carrying ``OrganizationScopedMixin`` has a
non-null, indexed ``organization_id``. Tables that do not carry it either are
the tenant (``organizations``), are global by design (``users``, ``tools``), or
reach their tenant through a parent (``messages``, ``workflow_steps``,
``workflow_step_runs``, ``agent_tools``) - duplicating the column there would
create a second source of truth that could disagree with the first.
"""

from __future__ import annotations

from app.models.agent import Agent, AgentTool, Tool
from app.models.approval import Approval
from app.models.audit import AuditEvent
from app.models.base import Base
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.enums import (
    ApprovalStatus,
    ConversationStatus,
    DocumentStatus,
    MemberRole,
    MembershipStatus,
    MessageRole,
    OrganizationStatus,
    RunStatus,
    StepRunStatus,
    UserStatus,
    WorkflowStepType,
)
from app.models.mixins import (
    CreatedAtMixin,
    OrganizationScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from app.models.workflow import Workflow, WorkflowRun, WorkflowStep, WorkflowStepRun

__all__ = [
    "Agent",
    "AgentTool",
    "Approval",
    "ApprovalStatus",
    "AuditEvent",
    "Base",
    "Conversation",
    "ConversationStatus",
    "CreatedAtMixin",
    "Document",
    "DocumentStatus",
    "MemberRole",
    "MembershipStatus",
    "Message",
    "MessageRole",
    "Organization",
    "OrganizationMember",
    "OrganizationScopedMixin",
    "OrganizationStatus",
    "RunStatus",
    "StepRunStatus",
    "TimestampMixin",
    "Tool",
    "UUIDPrimaryKeyMixin",
    "User",
    "UserStatus",
    "Workflow",
    "WorkflowRun",
    "WorkflowStep",
    "WorkflowStepRun",
    "WorkflowStepType",
]
