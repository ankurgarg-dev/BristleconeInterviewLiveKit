from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from livekit.agents import Agent


class BaseAgentFactory(ABC):
    """Factory interface for creating LiveKit Agent instances."""

    name: str

    @abstractmethod
    def create(self, metadata: dict[str, Any] | None = None) -> Agent:
        """Create and return a configured LiveKit Agent."""
        raise NotImplementedError
