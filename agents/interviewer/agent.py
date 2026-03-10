from __future__ import annotations

from typing import Any

from livekit.agents import Agent

from agents.base.agent_interface import BaseAgentFactory
from shared.prompts import INTERVIEWER_INSTRUCTIONS


class InterviewerAgent(Agent):
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        metadata = metadata or {}
        # TODO: Replace with interview plan, rubric, and evaluation state.
        instructions = metadata.get("instructions") or INTERVIEWER_INSTRUCTIONS
        super().__init__(instructions=instructions)


class InterviewerAgentFactory(BaseAgentFactory):
    name = "interviewer"

    def create(self, metadata: dict[str, Any] | None = None) -> Agent:
        return InterviewerAgent(metadata=metadata)
