"""Data access.

One repository per aggregate, all deriving from ``BaseRepository``. Services
call repositories; nothing else builds queries.
"""

from __future__ import annotations

from app.repositories.base import BaseRepository

__all__ = ["BaseRepository"]
