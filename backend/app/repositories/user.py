"""User data access.

Users are global, not tenant-owned: one person can belong to several
organizations. This repository therefore derives from ``BaseRepository`` rather
than the tenant-scoped one.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        """Find a user by their normalised address.

        The caller must pass an already-normalised address; addresses are
        stored lowercased so this is a plain equality match on the unique
        index rather than a function call the index could not serve.
        """
        result = await self.session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def email_exists(self, email: str) -> bool:
        return await self.get_by_email(email) is not None
