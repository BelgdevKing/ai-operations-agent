"""Redis client.

Used later for caching, rate limiting and the background job queue. For now it
backs the readiness check only, so it is optional in native development - see
``Settings.redis_required``. Connections are lazy: nothing here touches the
network at import or at startup, so the application boots with Redis absent.
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()
_client: Redis | None = None


def get_redis() -> Redis:
    """Return the shared Redis client, creating it on first use."""
    global _client
    if _client is None:
        _client = Redis.from_url(_settings.redis_url, decode_responses=True)
    return _client


async def check_redis() -> bool:
    """Return True when Redis answers PING.

    Never raises: an unreachable Redis is a reported state, not an error.
    """
    try:
        return bool(await get_redis().ping())
    except Exception as exc:
        # Readiness is polled continuously, so keep this quiet when Redis is
        # optional and keep the traceback at debug level either way.
        if _settings.redis_required:
            logger.warning("Redis is unavailable: %s", exc)
        else:
            logger.info("Redis is unavailable (optional in this configuration): %s", exc)
        logger.debug("Redis readiness check traceback", exc_info=True)
        return False


async def close_redis() -> None:
    """Close the client on shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
