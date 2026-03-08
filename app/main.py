from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import AgentServer, JobContext, cli, room_io

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.assistant.agent import AssistantAgentFactory
from agents.base.registry import AgentRegistry
from agents.interviewer.agent import InterviewerAgentFactory
from agents.support.agent import SupportAgentFactory
from shared.config import settings
from shared.utils import build_voice_session, parse_metadata, select_agent_name

load_dotenv()


def parse_args() -> tuple[str, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--agent", default=settings.default_agent)
    args, remaining = parser.parse_known_args(sys.argv[1:])
    return args.agent.lower(), remaining


def create_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register("assistant", AssistantAgentFactory)
    registry.register("support", SupportAgentFactory)
    registry.register("interviewer", InterviewerAgentFactory)
    return registry


async def rtc_entrypoint(ctx: JobContext) -> None:
    registry = create_registry()
    dispatch_metadata = parse_metadata(getattr(ctx.job, "metadata", None))
    room_metadata = parse_metadata(getattr(ctx.room, "metadata", None))

    default_agent = os.getenv("ACTIVE_AGENT", settings.default_agent)
    agent_name = select_agent_name(
        default_agent=default_agent,
        dispatch_metadata=dispatch_metadata,
        room_metadata=room_metadata,
    )

    selected_agent = registry.create(agent_name, metadata=dispatch_metadata)
    session = build_voice_session()
    await session.start(
        agent=selected_agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            close_on_disconnect=True,
            delete_room_on_close=True,
        ),
    )
    await session.say(
        "Hi, I am connected and listening. You can start speaking now.",
        allow_interruptions=True,
    )


def build_server() -> AgentServer:
    server = AgentServer(
        ws_url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )
    if settings.explicit_dispatch:
        server.rtc_session(rtc_entrypoint, agent_name=settings.dispatch_agent_name)
    else:
        server.rtc_session(rtc_entrypoint)
    return server


def main() -> None:
    selected_agent, passthrough = parse_args()
    os.environ["LIVEKIT_URL"] = settings.livekit_url
    os.environ["LIVEKIT_API_KEY"] = settings.livekit_api_key
    os.environ["LIVEKIT_API_SECRET"] = settings.livekit_api_secret
    os.environ["ACTIVE_AGENT"] = selected_agent

    # Forward remaining args (dev/start/console flags) to LiveKit CLI.
    sys.argv = [sys.argv[0], *passthrough]
    server = build_server()
    cli.run_app(server)


if __name__ == "__main__":
    main()
