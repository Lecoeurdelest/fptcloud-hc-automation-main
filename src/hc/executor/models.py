"""Data models for the Terraform executor."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCategory(StrEnum):
    """Classification of Terraform / provider errors."""

    TRANSIENT = "transient"  # retry
    QUOTA = "quota"  # DLQ immediately
    AUTH = "auth"  # DLQ + alert
    SCHEMA = "schema"  # DLQ — bad input, no point retrying
    UNKNOWN = "unknown"  # retry once, then DLQ


class ClassifiedError(BaseModel):
    category: ErrorCategory
    message: str
    raw: str


class TFResourceInstance(BaseModel):
    attributes: dict[str, Any] = Field(default_factory=dict)
    sensitive_attributes: list[Any] = Field(default_factory=list)


class TFResource(BaseModel):
    module: str | None = None
    mode: str = "managed"
    type: str = ""
    name: str = ""
    provider: str = ""
    instances: list[TFResourceInstance] = Field(default_factory=list)


class TFOutput(BaseModel):
    value: Any = None
    type: str | list[Any] | None = None
    sensitive: bool = False


class TFState(BaseModel):
    """Parsed output of ``terraform show -json``."""

    format_version: str = "1.0"
    terraform_version: str = ""
    resources: list[TFResource] = Field(default_factory=list)
    outputs: dict[str, TFOutput] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TFState:
        resources = [TFResource.model_validate(r) for r in data.get("resources", [])]
        outputs = {k: TFOutput.model_validate(v) for k, v in data.get("outputs", {}).items()}
        return cls(
            format_version=data.get("format_version", "1.0"),
            terraform_version=data.get("terraform_version", ""),
            resources=resources,
            outputs=outputs,
            raw=data,
        )

    def get_resource_attrs(
        self, resource_type: str, resource_name: str = "this"
    ) -> dict[str, Any] | None:
        """Return first instance attributes for a resource by type and name."""
        for res in self.resources:
            # Handles both bare resources and module-wrapped resources
            res_type = res.type
            res_name = res.name
            if res_type == resource_type and res_name == resource_name and res.instances:
                return res.instances[0].attributes
        return None

    def get_output(self, name: str) -> Any:
        """Return the value of a named output, or None."""
        out = self.outputs.get(name)
        return out.value if out is not None else None


class ExecutionResult(BaseModel):
    """Outcome of a full TerraformExecutor.execute() call."""

    success: bool
    plan_exit_code: int = 0  # 0=no-change, 2=changes, -1=error
    plan_json: dict[str, Any] = Field(default_factory=dict)
    state: TFState | None = None
    stdout: str = ""
    stderr: str = ""
    error: ClassifiedError | None = None
    workspace_path: str = ""
