"""JSON Schema for checklist.yml — P3.T1.

Validates the checklist document structure and rejects:
- missing required fields (run_id, tenant_id, test_cases)
- unknown top-level or test-case-level fields
- invalid CIDR format in spec.vars.cidr
"""

from __future__ import annotations

from typing import Any

import jsonschema

_CIDR_PATTERN = r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$"

_RETRY_POLICY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "max_attempts": {"type": "integer", "minimum": 1},
        "base_seconds": {"type": "integer", "minimum": 1},
        "max_seconds": {"type": "integer", "minimum": 1},
        "jitter": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

_EXPECTED_ASSERTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["type"],
    "additionalProperties": True,
    "properties": {
        "type": {
            "type": "string",
            "enum": ["tf_state", "in_vm", "api_probe", "manual"],
        },
        "path": {"type": "string"},
        "equals": {"type": "string"},
        "contains": {"type": "string"},
        "probe": {"type": "string"},
        "note": {"type": "string"},
    },
}

_SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action"],
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string"},
        "vars": {
            "type": "object",
            "additionalProperties": True,
            "patternProperties": {
                "^cidr$": {
                    "type": "string",
                    "pattern": _CIDR_PATTERN,
                }
            },
        },
        "gap": {"type": "string"},
    },
}

_TEST_CASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "category", "description", "spec", "expected"],
    "additionalProperties": False,
    "properties": {
        "id": {"oneOf": [{"type": "string"}, {"type": "integer", "minimum": 1}]},
        "category": {
            "type": "string",
            "enum": ["compute", "networking", "backup", "storage"],
        },
        "description": {"type": "string"},
        "spec": _SPEC_SCHEMA,
        "expected": {
            "type": "array",
            "minItems": 1,
            "items": _EXPECTED_ASSERTION_SCHEMA,
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
        },
        "gap": {"type": "string"},
    },
}

CHECKLIST_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["run_id", "tenant_id", "test_cases"],
    "additionalProperties": False,
    "properties": {
        "run_id": {"type": "string"},
        "tenant_id": {"type": "string"},
        "defaults": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "retry_policy": _RETRY_POLICY_SCHEMA,
            },
        },
        "test_cases": {
            "type": "array",
            "minItems": 1,
            "items": _TEST_CASE_SCHEMA,
        },
    },
}


class SchemaValidationError(ValueError):
    """Raised when the checklist document fails JSON Schema validation."""


def validate_checklist(data: dict[str, Any]) -> None:
    """Validate *data* against CHECKLIST_SCHEMA.

    Raises SchemaValidationError with a human-readable message that includes
    the JSON-path to the offending field, suitable for line-pointed reporting
    by callers that know the source file.
    """
    validator = jsonschema.Draft7Validator(CHECKLIST_SCHEMA)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        path = " → ".join(str(p) for p in first.absolute_path) or "(root)"
        raise SchemaValidationError(f"checklist validation error at {path}: {first.message}")
