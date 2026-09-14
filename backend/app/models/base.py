"""Declarative base for every ORM model.

The naming convention matters: without it, PostgreSQL invents names for
constraints and indexes, and Alembic autogenerate then produces migrations that
try to drop and recreate them. Setting it before the first table is created
means it never has to be retrofitted.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class all models inherit from."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        primary_key = getattr(self, "id", None)
        return f"<{type(self).__name__} id={primary_key!r}>"
