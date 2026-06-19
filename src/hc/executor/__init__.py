"""Terraform executor - Phase 2.

Exports are lazy so importing ``hc.executor.models`` does not require runtime
logging dependencies used by the concrete executor/classifier.
"""

from hc.executor.models import (
    ClassifiedError,
    ErrorCategory,
    ExecutionResult,
    TFOutput,
    TFResource,
    TFResourceInstance,
    TFState,
)

__all__ = [
    "ClassifiedError",
    "ErrorCategory",
    "ErrorClassifier",
    "ExecutionResult",
    "TFOutput",
    "TFResource",
    "TFResourceInstance",
    "TFState",
    "TerraformExecutor",
    "TerraformExecutorError",
]


def __getattr__(name: str):
    if name == "ErrorClassifier":
        from hc.executor.classifier import ErrorClassifier

        return ErrorClassifier
    if name in {"TerraformExecutor", "TerraformExecutorError"}:
        from hc.executor.executor import TerraformExecutor, TerraformExecutorError

        return {
            "TerraformExecutor": TerraformExecutor,
            "TerraformExecutorError": TerraformExecutorError,
        }[name]
    raise AttributeError(name)
