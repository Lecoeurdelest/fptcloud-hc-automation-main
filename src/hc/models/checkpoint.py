"""Checkpoint and validation models."""

from __future__ import annotations

from typing import Any, Literal, Optional

from hc.compat import StrEnum
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class ExpectedAssertion(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["tf_state", "in_vm", "api_probe", "manual"]
    path: Optional[str] = None
    equals: Optional[str] = None
    contains: Optional[str] = None
    regex_match: Optional[str] = None
    present: Optional[bool] = None
    absent: Optional[bool] = None
    probe: Optional[str] = None
    command: Optional[str] = None
    transport: Optional[str] = None
    os_type: Optional[str] = None
    host: Optional[str] = None
    host_path: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    private_key_path: Optional[str] = None
    exit_code: Optional[int] = None
    stdout_contains: Optional[str] = None
    file_exists: Optional[str] = None
    check: Optional[str] = None
    bucket: Optional[str] = None
    key: Optional[str] = None
    note: Optional[str] = None
    url: Optional[str] = None
    method: Optional[str] = None
    status_code: Optional[int] = None
    timeout_seconds: Optional[float] = None
    retries: Optional[int] = None
    tls_verify: Optional[bool] = None


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
