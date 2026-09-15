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


class WorkflowStatus(enum.StrEnum):
    """Lifecycle of one version of a workflow definition.

    Replaces the ``enabled`` boolean the schema phase gave ``workflows``, which
    could not tell "nobody has finished writing this" from "somebody turned it
    off". Only ``active`` versions may be started, and a draft is never run by
    accident.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"


class WorkflowStepType(enum.StrEnum):
    """The kinds of step a workflow can contain.

    These names are also the ``type`` discriminator in the stored definition
    document - see :mod:`app.workflows.definition`. One vocabulary for the
    document, the column and the engine; a second, friendlier set of names would
    only be a mapping to keep in step.
    """

    TOOL_CALL = "tool_call"
    AGENT_STEP = "agent_step"
    APPROVAL = "approval"
    CONDITION = "condition"


class RunStatus(enum.StrEnum):
    """State of a *workflow* run.

    Not the agent runtime's. An agent run's lifecycle is
    :class:`app.agents.models.AgentRunStatus`, and the two are near-twins that
    differ in one word: this one says "succeeded" where that one says
    "completed". The word is not worth a data migration to align, and both
    tables now carry ``awaiting_approval``, which is the state that actually
    matters - a run paused because a person was asked is not a run that
    finished.
    """

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepRunStatus(enum.StrEnum):
    """State of a single step within a run.

    Separate from :class:`RunStatus` because a step can be *skipped* - the
    branch a condition did not take - which a run cannot, and because a step
    waiting on a person has to be distinguishable from one that is running.
    """

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ApprovalStatus(enum.StrEnum):
    """State of a human approval request.

    Only one decision may ever be recorded, so the transition out of ``pending``
    is made by a conditional UPDATE rather than by reading the row and writing
    it back - see :class:`app.repositories.approval.ApprovalRepository`.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# -- Business operations ------------------------------------------------------


class CustomerStatus(enum.StrEnum):
    """Whether an account is currently traded with."""

    ACTIVE = "active"
    INACTIVE = "inactive"


class ShipmentStatus(enum.StrEnum):
    """Where a shipment is in its journey.

    Enumerated rather than free text so the agent and the application agree on
    what "delivered" means without either of them guessing.
    """

    PENDING = "pending"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    EXCEPTION = "exception"


class ChargeType(enum.StrEnum):
    """What a charge on a shipment is for."""

    FREIGHT = "freight"
    FUEL_SURCHARGE = "fuel_surcharge"
    CUSTOMS_DUTY = "customs_duty"
    HANDLING = "handling"
    STORAGE = "storage"
    INSURANCE = "insurance"


class ChargeStatus(enum.StrEnum):
    """Whether a charge is still owed.

    ``outstanding`` is the whole business rule: what a customer still owes on a
    shipment is the sum of the charges in this state.
    """

    OUTSTANDING = "outstanding"
    PAID = "paid"
    WAIVED = "waived"


class InvoiceStatus(enum.StrEnum):
    """Where an invoice is in its billing cycle.

    Deliberately no ``overdue``: that depends on today's date, and a status
    column recording it would be wrong the morning after it was written.
    Overdue is derived when it is needed.
    """

    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    CANCELLED = "cancelled"
