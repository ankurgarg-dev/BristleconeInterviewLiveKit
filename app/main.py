from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import AgentServer, JobContext, cli, room_io

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()
logger = logging.getLogger("app.main")

from agents.assistant.agent import AssistantAgentFactory
from agents.base.registry import AgentRegistry
from agents.interviewer.agent import InterviewerAgentFactory
from agents.observer.agent import ObserverAgentFactory
from agents.realtime.agent import RealtimeAgentFactory
from agents.support.agent import SupportAgentFactory
from shared.config import settings
from shared.utils import build_realtime_session, build_voice_session, parse_metadata, select_agent_name


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
    registry.register("realtime", RealtimeAgentFactory)
    registry.register("observer", ObserverAgentFactory)
    return registry


async def rtc_entrypoint(ctx: JobContext) -> None:
    dispatch_metadata = parse_metadata(getattr(ctx.job, "metadata", None))
    room_metadata = parse_metadata(getattr(ctx.room, "metadata", None))
    job_agent_name = getattr(ctx.job, "agent_name", "") or ""

    # Accept only explicit dispatch jobs for this worker.
    if settings.explicit_dispatch and job_agent_name != settings.dispatch_agent_name:
        logger.warning(
            "ignoring dispatch with unexpected job.agent_name=%s expected=%s",
            job_agent_name,
            settings.dispatch_agent_name,
        )
        ctx.shutdown("Ignoring dispatch with unexpected agent_name")
        return

    # Ignore wildcard/system dispatches that do not explicitly declare agent metadata.
    if settings.explicit_dispatch and "agent" not in dispatch_metadata:
        logger.warning("ignoring dispatch without explicit metadata: %s", getattr(ctx.job, "metadata", None))
        ctx.shutdown("Ignoring implicit dispatch without explicit agent metadata")
        return

    registry = create_registry()

    default_agent = os.getenv("ACTIVE_AGENT", settings.default_agent)
    agent_name = select_agent_name(
        default_agent=default_agent,
        dispatch_metadata=dispatch_metadata,
        room_metadata=room_metadata,
    )

    selected_agent = registry.create(agent_name, metadata=dispatch_metadata)
    if agent_name == "observer":
        # Presence-only agent mode: no LLM/STT/TTS path in worker.
        await ctx.connect()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return
        return

    session = build_realtime_session() if agent_name == "realtime" else build_voice_session()
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
