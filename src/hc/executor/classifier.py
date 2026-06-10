"""Error classifier — maps Terraform / FPT Cloud error text to actionable categories."""

from __future__ import annotations

import re

import structlog

from hc.executor.models import ClassifiedError, ErrorCategory

logger = structlog.get_logger()

# Ordered list of (category, [regex patterns]).  First match wins.
_PATTERNS: list[tuple[ErrorCategory, list[str]]] = [
    (
        ErrorCategory.AUTH,
        [
            r"(?i)401\b.*unauthorized",
            r"(?i)403\b.*forbidden",
            r"(?i)authentication\s+failed",
            r"(?i)invalid\s+(token|credential|api.?key)",
            r"(?i)access\s+denied",
            r"(?i)permission\s+denied",
            r"(?i)could\s+not\s+authenticate",
            r"(?i)unauthenticated",
        ],
    ),
    (
        ErrorCategory.QUOTA,
        [
            r"(?i)quota\s+exceeded",
            r"(?i)limit\s+exceeded",
            r"(?i)insufficient\s+(quota|capacity|resources?)",
            r"(?i)over\s+(?:the\s+)?limit",
            r"(?i)resource\s+limit\s+reached",
            r"(?i)429\b.*too\s+many\s+requests",
            r"(?i)rate\s+limit",
            r"(?i)max(imum)?\s+(?:number|count)\s+of",
        ],
    ),
    (
        ErrorCategory.SCHEMA,
        [
            r"(?i)invalid\s+configuration",
            r"(?i)schema\s+mismatch",
            r"(?i)unsupported\s+argument",
            r"(?i)unexpected\s+attribute",
            r"(?i)an\s+argument\s+named\s+.+\s+is\s+not\s+expected",
            r"(?i)unknown\s+variable",
            r"(?i)invalid\s+value\s+for",
            r"(?i)expected\s+type\s+to\s+be",
            r"(?i)value\s+must\s+be\s+one\s+of",
            r"(?i)no\s+such\s+resource\s+type",
            r"(?i)provider\s+does\s+not\s+support\s+resource\s+type",
        ],
    ),
    (
        ErrorCategory.TRANSIENT,
        [
            r"(?i)5\d{2}\s+(internal\s+server|bad\s+gateway|service\s+unavailable|gateway\s+timeout)",
            r"(?i)connection\s+(timed?\s*out|refused|reset|aborted)",
            r"(?i)network\s+(timeout|unreachable|error)",
            r"(?i)i/?o\s+timeout",
            r"(?i)eof\s+while\s+reading",
            r"(?i)state\s+(file\s+)?lock",
            r"(?i)lock\s+timeout",
            r"(?i)temporary\s+(failure|error)",
            r"(?i)context\s+deadline\s+exceeded",
            r"(?i)server\s+misbehaving",
            r"(?i)request\s+timed?\s*out",
            r"(?i)dial\s+tcp\b",
            r"(?i)unexpected\s+eof",
        ],
    ),
]


class ErrorClassifier:
    """Classify Terraform / provider error text into actionable categories."""

    def classify(self, error_text: str) -> ClassifiedError:
        for category, patterns in _PATTERNS:
            for pattern in patterns:
                if re.search(pattern, error_text):
                    return ClassifiedError(
                        category=category,
                        message=self._extract_message(error_text),
                        raw=error_text,
                    )

        logger.warning(
            "unclassified_terraform_error",
            hint="Add a pattern to src/hc/executor/classifier.py to suppress this warning",
            snippet=error_text[:200],
        )
        return ClassifiedError(
            category=ErrorCategory.UNKNOWN,
            message=self._extract_message(error_text),
            raw=error_text,
        )

    @staticmethod
    def _extract_message(error_text: str) -> str:
        """Return first meaningful line, capped at 256 chars."""
        for line in error_text.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:256]
        return error_text[:256]
