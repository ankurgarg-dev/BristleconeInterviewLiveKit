from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from livekit import api
from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest
from livekit.protocol.room import ListParticipantsRequest, ListRoomsRequest

from shared.config import settings


@dataclass(frozen=True)
class EnsureAgentResult:
    room_exists: bool
    has_humans: bool
    has_agent: bool
    had_valid_dispatch: bool
    created_dispatch: bool


def is_agent_participant(participant: Any) -> bool:
    identity = getattr(participant, "identity", "") or ""
    if identity.startswith("agent-"):
        return True

    permission = getattr(participant, "permission", None)
    if permission is not None and getattr(permission, "agent", False):
        return True

    # ParticipantKind.AGENT enum value in LiveKit protocol.
    return getattr(participant, "kind", None) == 4


def build_dispatch_metadata(agent: str, instructions: str | None = None) -> dict[str, str]:
    metadata: dict[str, str] = {
        "agent": agent,
        "room_mode": "human_ai",
    }
    if instructions:
        metadata["instructions"] = instructions
    return metadata


async def ensure_agent_for_room(
    room: str,
    agent: str,
    instructions: str | None = None,
) -> EnsureAgentResult:
    metadata = build_dispatch_metadata(agent=agent, instructions=instructions)

    async with api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    ) as lk:
        rooms = await lk.room.list_rooms(ListRoomsRequest(names=[room]))
        if not rooms.rooms:
            return EnsureAgentResult(
                room_exists=False,
                has_humans=False,
                has_agent=False,
                had_valid_dispatch=False,
                created_dispatch=False,
            )

        participants = await lk.room.list_participants(ListParticipantsRequest(room=room))
        agent_participants = [p for p in participants.participants if is_agent_participant(p)]
        human_participants = [p for p in participants.participants if not is_agent_participant(p)]

        dispatches = await lk.agent_dispatch.list_dispatch(room)
        valid_dispatches = [
            dispatch for dispatch in dispatches if getattr(dispatch, "agent_name", "") == settings.dispatch_agent_name
        ]

        should_create_dispatch = bool(human_participants) and not agent_participants and not valid_dispatches
        if should_create_dispatch:
            await lk.agent_dispatch.create_dispatch(
                CreateAgentDispatchRequest(
                    agent_name=settings.dispatch_agent_name,
                    room=room,
                    metadata=json.dumps(metadata),
                )
            )

        return EnsureAgentResult(
            room_exists=True,
            has_humans=bool(human_participants),
            has_agent=bool(agent_participants),
            had_valid_dispatch=bool(valid_dispatches),
            created_dispatch=should_create_dispatch,
        )
