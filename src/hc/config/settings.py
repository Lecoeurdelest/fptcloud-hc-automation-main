"""Runtime configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _split_env_list(raw: str) -> list[str]:
    values: list[str] = []
    for item in raw.replace("\n", ",").split(","):
        value = item.strip()
        if value:
            values.append(value)
    return values


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return int(raw)


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class CloudSettings:
    api_url: str = ""
    region: str = ""
    tenant_name: str = ""
    token: str = ""
    vpc_ids: tuple[str, ...] = ()

    @property
    def vpc_id(self) -> str:
        return self.vpc_ids[0] if self.vpc_ids else ""

    @classmethod
    def from_env(cls) -> CloudSettings:
        raw_vpc_ids = os.environ.get("VPC_IDS")
        vpc_ids = _split_env_list(raw_vpc_ids) if raw_vpc_ids else []
        legacy_vpc_id = os.environ.get("VPC_ID", "").strip()
        if legacy_vpc_id:
            vpc_ids.append(legacy_vpc_id)

        return cls(
            api_url=_env_str("FPTCLOUD_API_URL", ""),
            region=_env_str("FPTCLOUD_REGION", ""),
            tenant_name=_env_str("FPTCLOUD_TENANT_NAME", ""),
            token=_env_str("FPTCLOUD_TOKEN", ""),
            vpc_ids=tuple(_dedupe_keep_order(vpc_ids)),
        )

    def terraform_env(self) -> dict[str, str]:
        env = {}
        if self.api_url:
            env["FPTCLOUD_API_URL"] = self.api_url
        if self.region:
            env["FPTCLOUD_REGION"] = self.region
        if self.tenant_name:
            env["FPTCLOUD_TENANT_NAME"] = self.tenant_name
        if self.token:
            env["FPTCLOUD_TOKEN"] = self.token
        return env

    def terraform_vars_by_vpc(self, base_vars: dict[str, object]) -> tuple[dict[str, object], ...]:
        return tuple({**base_vars, "vpc_id": vpc_id} for vpc_id in self.vpc_ids)


@dataclass(frozen=True)
class QueueSettings:
    redis_url: str = "redis://localhost:6379/0"
    consumer_group: str = "hc-workers"
    reaper_idle_ms: int = 300_000
    max_attempts: int = 3
    stream_tasks: str = "hc:tasks"
    stream_dlq: str = "hc:dlq"
    zset_dedup: str = "hc:dedup"
    zset_scheduled: str = "hc:scheduled"

    @classmethod
    def from_env(cls) -> QueueSettings:
        return cls(
            redis_url=_env_str("REDIS_URL", "redis://localhost:6379/0"),
            consumer_group=_env_str("HC_CONSUMER_GROUP", "hc-workers"),
            reaper_idle_ms=_env_int("HC_REAPER_IDLE_MS", 300_000),
            max_attempts=_env_int("HC_MAX_ATTEMPTS", 3),
        )
