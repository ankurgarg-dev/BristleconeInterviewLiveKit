from __future__ import annotations

from typing import Any

from livekit.agents import Agent

from agents.base.agent_interface import BaseAgentFactory
from shared.prompts import OBSERVER_INSTRUCTIONS


class ObserverAgent(Agent):
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        metadata = metadata or {}
        instructions = metadata.get("instructions") or OBSERVER_INSTRUCTIONS
        super().__init__(instructions=instructions)


class ObserverAgentFactory(BaseAgentFactory):
    name = "observer"

    def create(self, metadata: dict[str, Any] | None = None) -> Agent:
        return ObserverAgent(metadata=metadata)
