"""Task queue models."""

from __future__ import annotations

import hashlib
import json
import random
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class EnqueueResult(StrEnum):
    ENQUEUED = "Enqueued"
    DUPLICATE = "Duplicate"


class RetryPolicy(BaseModel):
    max_attempts: int = 3
    base_seconds: int = 30
    max_seconds: int = 600
    jitter: float = 0.2

    @field_validator("max_seconds")
    @classmethod
    def max_gte_base(cls, v: int, info: Any) -> int:
        base = info.data.get("base_seconds", 30)
        if v < base:
            msg = "max_seconds must be >= base_seconds"
            raise ValueError(msg)
        return v


class TaskSpec(BaseModel):
    run_id: str
    tc_id: str
    tenant_id: str
    spec_hash: str
    task_id: str = ""
    category: str = "compute"
    description: str = ""
    spec: dict[str, Any] = Field(default_factory=dict)
    attempt: int = 0
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    enqueued_at: float | None = None
    parent_task_id: str | None = None

    @model_validator(mode="after")
    def compute_task_id(self) -> TaskSpec:
        if not self.task_id:
            self.task_id = compute_task_id(self.run_id, self.tc_id, self.tenant_id, self.spec_hash)
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.spec, sort_keys=True, separators=(",", ":"))

    def to_stream_fields(self) -> dict[str, str]:
        return {"payload": self.model_dump_json()}

    @classmethod
    def from_stream_fields(cls, fields: dict[str, str]) -> TaskSpec:
        raw = fields.get("payload", "{}")
        return cls.model_validate_json(raw)


class QueueEntry(BaseModel):
    entry_id: str
    task: TaskSpec


def compute_task_id(run_id: str, tc_id: str, tenant_id: str, spec_hash: str) -> str:
    material = f"{run_id}/{tc_id}/{tenant_id}/{spec_hash}"
    return hashlib.sha256(material.encode()).hexdigest()


def compute_spec_hash(spec: dict[str, Any]) -> str:
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def compute_backoff_seconds(attempt: int, policy: RetryPolicy) -> float:
    """Exponential backoff with jitter, capped at max_seconds."""
    base = policy.base_seconds * (2 ** max(attempt - 1, 0))
    capped = min(base, policy.max_seconds)
    jitter_range = capped * policy.jitter
    jitter = random.uniform(-jitter_range, jitter_range)
    return float(max(0.0, capped + jitter))


def now_ms() -> int:
    return int(time.time() * 1000)
