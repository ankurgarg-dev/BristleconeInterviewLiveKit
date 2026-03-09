from __future__ import annotations

from typing import Any

from livekit.agents import Agent

from agents.base.agent_interface import BaseAgentFactory
from shared.prompts import REALTIME_INSTRUCTIONS


class RealtimeAgent(Agent):
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        metadata = metadata or {}
        instructions = metadata.get("instructions") or REALTIME_INSTRUCTIONS
        super().__init__(instructions=instructions)


class RealtimeAgentFactory(BaseAgentFactory):
    name = "realtime"

    def create(self, metadata: dict[str, Any] | None = None) -> Agent:
        return RealtimeAgent(metadata=metadata)
