"""Model representing a saved application setting."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Setting:
    id: int | None
    key: str
    value: str | None
