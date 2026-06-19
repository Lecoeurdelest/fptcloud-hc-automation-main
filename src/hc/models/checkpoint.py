"""Checkpoint and validation models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class ExpectedAssertion(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["tf_state", "in_vm", "api_probe", "manual"]
    path: str | None = None
    equals: str | None = None
    contains: str | None = None
    probe: str | None = None
    check: str | None = None
    bucket: str | None = None
    key: str | None = None
    note: str | None = None
    url: str | None = None
    status_code: int | None = None


class Checkpoint(BaseModel):
    id: str
    category: Literal["compute", "networking", "backup", "storage"]
    description: str
    spec: dict[str, Any] = Field(default_factory=dict)
    expected: list[ExpectedAssertion] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        allowed = {"compute", "networking", "backup", "storage"}
        if v not in allowed:
            msg = f"unknown category: {v}"
            raise ValueError(msg)
        return v
