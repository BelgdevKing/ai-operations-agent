"""Uploaded documents.

Metadata only. No upload handling, text extraction, chunking or embedding -
those arrive with the retrieval phase. ``status`` is the hook that pipeline
will drive.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import DocumentStatus, enum_column
from app.models.mixins import OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.user import User


class Document(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """A file belonging to an organization."""

    __tablename__ = "documents"

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        # SET NULL: the document outlives the account that uploaded it.
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(200), nullable=False)

    # Path or key in the object store. Unique because two rows pointing at the
    # same blob would make deletion unsafe.
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)

    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[DocumentStatus] = mapped_column(
        enum_column(DocumentStatus, "status"),
        nullable=False,
        default=DocumentStatus.PENDING,
        server_default=DocumentStatus.PENDING.value,
    )

    organization: Mapped[Organization] = relationship()
    uploader: Mapped[User | None] = relationship()

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_bytes_non_negative"),
        Index("ix_documents_uploaded_by", "uploaded_by"),
        # The ingestion worker polls for pending documents per tenant.
        Index("ix_documents_organization_id_status", "organization_id", "status"),
    )
