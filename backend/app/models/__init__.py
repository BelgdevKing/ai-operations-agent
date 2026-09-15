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
from app.models.agent_run import (
    AgentRunRecord,
    # Defined by the agent runtime and re-exported here: the durable status
    # column and the runtime's lifecycle are the same enum, not two copies.
    AgentRunStatus,
    AgentStepRecord,
    ToolExecutionRecord,
)
from app.models.approval import Approval
from app.models.audit import AuditEvent
from app.models.base import Base
from app.models.business import Customer, Invoice, Shipment, ShipmentCharge
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.enums import (
    ApprovalStatus,
    ChargeStatus,
    ChargeType,
    ConversationStatus,
    CustomerStatus,
    DocumentStatus,
    InvoiceStatus,
    MemberRole,
    MembershipStatus,
    MessageRole,
    OrganizationStatus,
    RunStatus,
    ShipmentStatus,
    StepRunStatus,
    UserStatus,
    WorkflowStatus,
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
    "AgentRunRecord",
    "AgentRunStatus",
    "AgentStepRecord",
    "AgentTool",
    "Approval",
    "ApprovalStatus",
    "AuditEvent",
    "Base",
    "ChargeStatus",
    "ChargeType",
    "Conversation",
    "ConversationStatus",
    "CreatedAtMixin",
    "Customer",
    "CustomerStatus",
    "Document",
    "DocumentStatus",
    "Invoice",
    "InvoiceStatus",
    "MemberRole",
    "MembershipStatus",
    "Message",
    "MessageRole",
    "Organization",
    "OrganizationMember",
    "OrganizationScopedMixin",
    "OrganizationStatus",
    "RunStatus",
    "Shipment",
    "ShipmentCharge",
    "ShipmentStatus",
    "StepRunStatus",
    "TimestampMixin",
    "Tool",
    "ToolExecutionRecord",
    "UUIDPrimaryKeyMixin",
    "User",
    "UserStatus",
    "Workflow",
    "WorkflowRun",
    "WorkflowStatus",
    "WorkflowStep",
    "WorkflowStepRun",
    "WorkflowStepType",
]
