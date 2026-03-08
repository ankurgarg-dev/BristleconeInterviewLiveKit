from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from livekit import api
from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.config import settings

load_dotenv()


async def dispatch(room: str, agent: str, instructions: str | None = None) -> None:
    metadata = {"agent": agent}
    if instructions:
        metadata["instructions"] = instructions

    async with api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    ) as lk:
        result = await lk.agent_dispatch.create_dispatch(
            CreateAgentDispatchRequest(
                agent_name=settings.dispatch_agent_name,
                room=room,
                metadata=json.dumps(metadata),
            )
        )

    print(f"dispatch_id={result.id}")
    print(f"room={result.room}")
    print(f"dispatch_agent_name={result.agent_name}")
    print(f"metadata={result.metadata}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch a LiveKit agent into a room")
    parser.add_argument("--room", required=True, help="Target room")
    parser.add_argument("--agent", default=settings.default_agent, help="Logical agent (assistant/support/interviewer)")
    parser.add_argument("--instructions", default=None, help="Optional runtime instructions override")
    args = parser.parse_args()

    asyncio.run(dispatch(room=args.room, agent=args.agent, instructions=args.instructions))


if __name__ == "__main__":
    main()
