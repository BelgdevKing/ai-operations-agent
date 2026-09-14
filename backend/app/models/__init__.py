"""SQLAlchemy models.

Every model module must be imported here. Alembic autogenerate compares the
database against ``Base.metadata``, and a model that is never imported is not
in that metadata - so its table would be silently dropped from migrations.
"""

from __future__ import annotations

from app.models.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

__all__ = ["Base", "TimestampMixin", "UUIDPrimaryKeyMixin"]
