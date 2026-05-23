"""Redis service for caching and pub/sub."""

import json
from typing import Any, Optional
from redis.asyncio import Redis, ConnectionPool
from config.settings import settings
from logging.logger import get_logger

log = get_logger(__name__)

_redis_pool: Optional[ConnectionPool] = None
_redis_client: Optional[Redis] = None


async def get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis(
            connection_pool=ConnectionPool.from_url(
                settings.redis_url,
                decode_responses=False,
                max_connections=20,
            )
        )
    return _redis_client


async def close_redis() -> None:
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None


class CacheService:
    """Redis-backed cache with JSON serialization."""

    def __init__(self, client: Redis):
        self._client = client

    async def get(self, key: str) -> Optional[dict]:
        data = await self._client.get(key)
        if data:
            return json.loads(data)
        return None

    async def set(self, key: str, value: dict, ttl: int = 60) -> None:
        await self._client.setex(key, ttl, json.dumps(value, default=str))

    async def incr(self, key: str, amount: int = 1) -> int:
        return await self._client.incrby(key, amount)

    async def expire(self, key: str, ttl: int) -> None:
        await self._client.expire(key, ttl)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def hset(self, name: str, key: str, value: Any) -> None:
        await self._client.hset(name, key, json.dumps(value, default=str))

    async def hget(self, name: str, key: str) -> Optional[dict]:
        data = await self._client.hget(name, key)
        if data:
            return json.loads(data)
        return None

    async def hgetall(self, name: str) -> dict:
        result = await self._client.hgetall(name)
        return {k: json.loads(v) for k, v in result.items()}

    async def publish(self, channel: str, message: dict) -> None:
        await self._client.publish(channel, json.dumps(message, default=str))


class RateLimiter:
    """Token bucket rate limiter using Redis."""

    def __init__(self, client: Redis, key: str, rate: int, per: int):
        self._client = client
        self._key = key
        self._rate = rate
        self._per = per

    async def is_allowed(self) -> bool:
        """Returns True if the request is allowed under rate limit."""
        key = f"ratelimit:{self._key}"
        count = await self._client.get(key)
        if count is None:
            await self._client.setex(key, self._per, 1)
            return True
        if int(count) >= self._rate:
            return False
        await self._client.incr(key)
        return True

    async def wait_time(self) -> int:
        """Returns seconds until rate limit resets."""
        ttl = await self._client.ttl(f"ratelimit:{self._key}")
        return max(0, ttl)
