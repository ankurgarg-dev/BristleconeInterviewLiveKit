from __future__ import annotations

from typing import Any

from livekit.agents import Agent

from agents.base.agent_interface import BaseAgentFactory
from shared.prompts import ASSISTANT_INSTRUCTIONS


class AssistantAgent(Agent):
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        metadata = metadata or {}
        instructions = metadata.get("instructions") or ASSISTANT_INSTRUCTIONS
        super().__init__(instructions=instructions)


class AssistantAgentFactory(BaseAgentFactory):
    name = "assistant"

    def create(self, metadata: dict[str, Any] | None = None) -> Agent:
        return AssistantAgent(metadata=metadata)
