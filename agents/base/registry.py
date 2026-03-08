from __future__ import annotations

from typing import Any

from agents.base.agent_interface import BaseAgentFactory


class AgentRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, type[BaseAgentFactory]] = {}

    def register(self, name: str, factory_cls: type[BaseAgentFactory]) -> None:
        key = name.strip().lower()
        self._factories[key] = factory_cls

    def get_factory(self, name: str) -> BaseAgentFactory:
        key = name.strip().lower()
        factory_cls = self._factories.get(key)
        if factory_cls is None:
            available = ", ".join(sorted(self._factories.keys())) or "none"
            raise ValueError(f"Unknown agent '{name}'. Available agents: {available}")
        return factory_cls()

    def create(self, name: str, metadata: dict[str, Any] | None = None):
        return self.get_factory(name).create(metadata)

    def names(self) -> list[str]:
        return sorted(self._factories.keys())
