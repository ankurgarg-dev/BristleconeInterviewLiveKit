from __future__ import annotations

from typing import Any

from livekit.agents import Agent

from agents.base.agent_interface import BaseAgentFactory
from shared.prompts import SUPPORT_INSTRUCTIONS


class SupportAgent(Agent):
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        # TODO: Replace with support-specific tools/workflows.
        super().__init__(instructions=SUPPORT_INSTRUCTIONS)


class SupportAgentFactory(BaseAgentFactory):
    name = "support"

    def create(self, metadata: dict[str, Any] | None = None) -> Agent:
        return SupportAgent(metadata=metadata)
