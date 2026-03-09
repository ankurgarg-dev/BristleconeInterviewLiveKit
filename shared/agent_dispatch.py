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


@dataclass(frozen=True)
class PrepareObserverRoomResult:
    room_exists: bool
    blocked_by_humans: bool
    removed_dispatches: int
    removed_agents: int
    human_identities: tuple[str, ...]


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


def _dispatch_matches_agent(dispatch: Any, expected_agent: str) -> bool:
    if getattr(dispatch, "agent_name", "") != settings.dispatch_agent_name:
        return False
    raw_metadata = getattr(dispatch, "metadata", None)
    if not raw_metadata:
        return False
    try:
        parsed = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    return str(parsed.get("agent", "")).lower() == expected_agent.lower()


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
        valid_dispatches = [dispatch for dispatch in dispatches if _dispatch_matches_agent(dispatch, agent)]

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


async def prepare_observer_room(room: str) -> PrepareObserverRoomResult:
    async with api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    ) as lk:
        rooms = await lk.room.list_rooms(ListRoomsRequest(names=[room]))
        if not rooms.rooms:
            return PrepareObserverRoomResult(
                room_exists=False,
                blocked_by_humans=False,
                removed_dispatches=0,
                removed_agents=0,
                human_identities=(),
            )

        participants = await lk.room.list_participants(ListParticipantsRequest(room=room))
        agent_participants = [p for p in participants.participants if is_agent_participant(p)]
        human_participants = [p for p in participants.participants if not is_agent_participant(p)]
        human_identities = tuple((getattr(p, "identity", "") or "").strip() for p in human_participants)
        if human_participants:
            return PrepareObserverRoomResult(
                room_exists=True,
                blocked_by_humans=True,
                removed_dispatches=0,
                removed_agents=0,
                human_identities=tuple(identity for identity in human_identities if identity),
            )

        dispatches = await lk.agent_dispatch.list_dispatch(room)
        removed_dispatches = 0
        for dispatch in dispatches:
            dispatch_id = getattr(dispatch, "id", "") or ""
            if not dispatch_id:
                continue
            await lk.agent_dispatch.delete_dispatch(dispatch_id=dispatch_id, room_name=room)
            removed_dispatches += 1

        removed_agents = 0
        for participant in agent_participants:
            identity = getattr(participant, "identity", "") or ""
            if not identity:
                continue
            await lk.room.remove_participant(api.RoomParticipantIdentity(room=room, identity=identity))
            removed_agents += 1

        return PrepareObserverRoomResult(
            room_exists=True,
            blocked_by_humans=False,
            removed_dispatches=removed_dispatches,
            removed_agents=removed_agents,
            human_identities=(),
        )
