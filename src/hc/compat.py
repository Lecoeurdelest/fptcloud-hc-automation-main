"""Compatibility helpers for supported and local development Python versions."""

from __future__ import annotations

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        """Python <3.11 fallback matching the value behavior of enum.StrEnum."""

        def __str__(self) -> str:
            return str(self.value)


__all__ = ["StrEnum"]
