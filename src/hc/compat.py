"""Compatibility helpers for supported and local development Python versions."""

from __future__ import annotations

from datetime import timezone

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        """Python <3.11 fallback matching the value behavior of enum.StrEnum."""

        def __str__(self) -> str:
            return str(self.value)


try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc


__all__ = ["StrEnum", "UTC"]
