"""Shared pytest fixtures."""

from __future__ import annotations

import os

import fakeredis
import pytest
import redis

from hc.config.settings import QueueSettings
from hc.queue.redis_queue import RedisQueue


@pytest.fixture
def queue_settings() -> QueueSettings:
    return QueueSettings(
        redis_url="redis://localhost:6379/0",
        consumer_group="hc-workers-test",
        reaper_idle_ms=1000,
        max_attempts=3,
    )


@pytest.fixture
def fake_redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def queue(fake_redis: fakeredis.FakeRedis, queue_settings: QueueSettings) -> RedisQueue:
    q = RedisQueue(fake_redis, queue_settings)
    q.flush_all()
    yield q
    q.flush_all()


@pytest.fixture
def integration_redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def integration_queue(integration_redis_url: str) -> RedisQueue:
    """Real Redis when available; fakeredis fallback for local dev without Docker."""
    use_fake = os.environ.get("HC_USE_FAKEREDIS", "").lower() in ("1", "true", "yes")
    if use_fake:
        client: redis.Redis[str] | fakeredis.FakeRedis = fakeredis.FakeRedis(  # type: ignore[assignment]
            decode_responses=True
        )
        settings = QueueSettings(consumer_group="hc-workers-int")
    else:
        try:
            client = redis.from_url(integration_redis_url, decode_responses=True)
            client.ping()
        except (redis.ConnectionError, redis.TimeoutError):
            client = fakeredis.FakeRedis(decode_responses=True)  # type: ignore[assignment]
            use_fake = True
        settings = QueueSettings(
            redis_url=integration_redis_url if not use_fake else "fakeredis://",
            consumer_group="hc-workers-int",
        )
    q = RedisQueue(client, settings)
    q.flush_all()
    yield q
    q.flush_all()
