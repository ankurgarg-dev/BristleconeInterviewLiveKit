from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import certifi
from dotenv import load_dotenv
from livekit.agents import AgentServer, JobContext, JobExecutorType, cli, room_io

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()
logger = logging.getLogger("app.main")
worker_transcript_lock = threading.Lock()
worker_transcript_seen: dict[str, set[str]] = {}

from agents.assistant.agent import AssistantAgentFactory
from agents.base.registry import AgentRegistry
from agents.interviewer.agent import InterviewerAgentFactory
from agents.observer.agent import ObserverAgentFactory
from agents.realtime.agent import RealtimeAgentFactory
from agents.support.agent import SupportAgentFactory
from shared.config import settings
from shared.utils import build_realtime_session, build_voice_session, parse_metadata, select_agent_name


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned[:128] or "room"


def _worker_transcript_path(room: str) -> Path:
    safe_room = _safe_filename_component(room)
    return Path(__file__).resolve().parents[1] / "data" / "transcripts" / f"{safe_room}.jsonl"


def _append_worker_transcript(*, room: str, speaker: str, text: str, source: str, unique_key: str) -> None:
    line = {
        "timestamp": datetime.now(UTC).isoformat(),
        "room": room,
        "speaker": speaker,
        "source": source,
        "text": text,
        "username": "agent-worker",
    }

    with worker_transcript_lock:
        seen = worker_transcript_seen.setdefault(room, set())
        if unique_key in seen:
            return
        seen.add(unique_key)

        path = _worker_transcript_path(room)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(line, ensure_ascii=False) + "\n")


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

    # In explicit dispatch mode, prefer routed jobs but keep compatibility with
    # implicit jobs when Agent Dispatch service is unavailable.
    if settings.explicit_dispatch and job_agent_name and job_agent_name != settings.dispatch_agent_name:
        logger.warning(
            "ignoring dispatch with unexpected job.agent_name=%s expected=%s",
            job_agent_name,
            settings.dispatch_agent_name,
        )
        ctx.shutdown("Ignoring dispatch with unexpected agent_name")
        return

    # For explicitly routed jobs, require explicit metadata.
    if (
        settings.explicit_dispatch
        and settings.dispatch_agent_name
        and job_agent_name == settings.dispatch_agent_name
        and "agent" not in dispatch_metadata
    ):
        logger.warning("ignoring dispatch without explicit metadata: %s", getattr(ctx.job, "metadata", None))
        ctx.shutdown("Ignoring implicit dispatch without explicit agent metadata")
        return

    registry = create_registry()
    room_name = (getattr(ctx.room, "name", "") or "").strip()

    default_agent = os.getenv("ACTIVE_AGENT", settings.default_agent)
    agent_name = select_agent_name(
        default_agent=default_agent,
        dispatch_metadata=dispatch_metadata,
        room_metadata=room_metadata,
    )

    # Hard isolation: observer-designated rooms must never run worker voice agents.
    if room_name.endswith("-observer") and agent_name != "observer":
        logger.warning(
            "rejecting non-observer agent in observer room room=%s selected_agent=%s metadata=%s",
            room_name,
            agent_name,
            dispatch_metadata,
        )
        ctx.shutdown("Observer room accepts only observer agent")
        return

    selected_agent = registry.create(agent_name, metadata=dispatch_metadata)
    if agent_name == "observer":
        # Keep legacy observer rooms presence-only.
        await ctx.connect()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return
        return

    session = build_realtime_session() if agent_name == "realtime" else build_voice_session()

    @session.on("conversation_item_added")
    def _on_conversation_item_added(event) -> None:
        item = getattr(event, "item", None)
        if item is None:
            return
        role = getattr(item, "role", "")
        text = (getattr(item, "text_content", None) or "").strip()
        if role not in ("user", "assistant") or not text:
            return

        room_name = getattr(ctx.room, "name", "") or "room"
        speaker = "User" if role == "user" else f"{agent_name.title()} Agent"
        unique_key = f"worker:{role}:{getattr(item, 'id', '') or text}"
        _append_worker_transcript(
            room=room_name,
            speaker=speaker,
            text=text,
            source=f"agent-{agent_name}",
            unique_key=unique_key,
        )

    await session.start(
        agent=selected_agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            close_on_disconnect=True,
            delete_room_on_close=True,
        ),
    )
    # Realtime sessions do not include a standalone TTS model, so calling
    # session.say() raises runtime errors and tears down the job.
    if agent_name != "realtime":
        await session.say(
            "Hi, I am connected and listening. You can start speaking now.",
            allow_interruptions=True,
        )


def build_server() -> AgentServer:
    server = AgentServer(
        # Process executor mode is creating multiple schedulable worker IDs in
        # this local setup, and one of them intermittently blackholes jobs.
        # Thread mode keeps a single healthy scheduling path for demo stability.
        job_executor_type=JobExecutorType.THREAD,
        ws_url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
        # Keep worker schedulable on busy local dev laptops where the default
        # production threshold (0.7) can mark it unavailable continuously.
        load_threshold=0.99,
    )
    # In explicit-dispatch mode, register the worker under a named agent so
    # LiveKit does not also create implicit wildcard jobs for the same room.
    registered_agent_name = settings.dispatch_agent_name if settings.explicit_dispatch else ""
    server.rtc_session(rtc_entrypoint, agent_name=registered_agent_name)
    return server


def main() -> None:
    selected_agent, passthrough = parse_args()
    # Ensure outbound TLS (including aiohttp inside OpenAI realtime plugin)
    # can find a CA bundle in managed/local Python environments.
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
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
