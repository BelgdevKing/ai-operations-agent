"""Enumerations used by the schema.

Stored as ``VARCHAR`` with a ``CHECK`` constraint rather than a native
PostgreSQL ``ENUM`` type. Native enums need a migration to add a single value
and cannot drop one at all; a check constraint is rewritten in place, which
matters for statuses that will gain states as features land.

Values are lowercase strings. ``StrEnum`` means a member compares equal to its
value, so callers can pass either the member or the plain string.
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum as SAEnum


def _values(enum_class: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_class]


def enum_column(enum_class: type[enum.Enum], name: str) -> SAEnum:
    """Build the column type for an enum.

    ``values_callable`` is essential: without it SQLAlchemy persists the member
    *name* (``"ACTIVE"``) instead of its value (``"active"``).
    """
    return SAEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=_values,
    )


class OrganizationStatus(enum.StrEnum):
    """Lifecycle of a tenant."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class UserStatus(enum.StrEnum):
    """Lifecycle of an account. Authentication itself is not implemented yet."""

    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"


class MemberRole(enum.StrEnum):
    """A user's role within one organization."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class MembershipStatus(enum.StrEnum):
    """Lifecycle of a membership, including the invite that precedes it."""

    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class ConversationStatus(enum.StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class MessageRole(enum.StrEnum):
    """Author of a message, matching the roles Claude's API uses."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class DocumentStatus(enum.StrEnum):
    """Ingestion state. Processing itself arrives with the retrieval phase."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class WorkflowStepType(enum.StrEnum):
    """The kinds of step a workflow can contain."""

    TOOL_CALL = "tool_call"
    AGENT_STEP = "agent_step"
    APPROVAL = "approval"
    CONDITION = "condition"


class RunStatus(enum.StrEnum):
    """State of a workflow run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepRunStatus(enum.StrEnum):
    """State of a single step within a run.

    Separate from :class:`RunStatus` because a step can be skipped - by a
    condition, or because an earlier step failed - which a run cannot.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApprovalStatus(enum.StrEnum):
    """State of a human approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
