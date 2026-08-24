import json
import os

import redis

REDIS_URL = os.getenv("REDIS_URL")

# Redis is optional: the app runs fine without it (cache miss == hit the DB
# every time), which matters for local dev where you may not have Redis
# running. In production (Docker Compose / Render + a managed Redis) it's
# always set.
_client = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None

INVENTORY_CACHE_KEY = "inventory:list"
INVENTORY_CACHE_TTL_SECONDS = 15


def get_cached_inventory():
    if _client is None:
        return None
    try:
        raw = _client.get(INVENTORY_CACHE_KEY)
        return json.loads(raw) if raw else None
    except redis.RedisError:
        # Cache is a performance optimization, not a source of truth --
        # never let a Redis outage take down the API.
        return None


def set_cached_inventory(items):
    if _client is None:
        return
    try:
        _client.setex(INVENTORY_CACHE_KEY, INVENTORY_CACHE_TTL_SECONDS, json.dumps(items))
    except redis.RedisError:
        pass


def invalidate_inventory_cache():
    if _client is None:
        return
    try:
        _client.delete(INVENTORY_CACHE_KEY)
    except redis.RedisError:
        pass
