from __future__ import annotations

import json
import ssl
from typing import Any

import aiohttp
import certifi
from livekit.agents import AgentSession
from livekit.plugins import deepgram, openai, silero

from shared.config import settings

_realtime_http_sessions_by_loop: dict[int, aiohttp.ClientSession] = {}


def _get_realtime_http_session() -> aiohttp.ClientSession:
    try:
        import asyncio

        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = -1

    session = _realtime_http_sessions_by_loop.get(loop_id)
    if session is None or session.closed:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        session = aiohttp.ClientSession(connector=connector, trust_env=True)
        _realtime_http_sessions_by_loop[loop_id] = session

    return session


def parse_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    return {}


def select_agent_name(
    default_agent: str,
    dispatch_metadata: dict[str, Any] | None = None,
    room_metadata: dict[str, Any] | None = None,
) -> str:
    dispatch_metadata = dispatch_metadata or {}
    room_metadata = room_metadata or {}
    return str(
        dispatch_metadata.get("agent")
        or room_metadata.get("agent")
        or default_agent
    ).lower()


def build_voice_session() -> AgentSession:
    """Shared model setup for voice pipeline (STT -> LLM -> TTS)."""
    deepgram_key = (settings.deepgram_api_key or "").strip()
    use_deepgram = bool(deepgram_key and deepgram_key != "your_deepgram_key")

    # Fallback to OpenAI STT when Deepgram key is not configured.
    stt_impl = (
        deepgram.STT(model=settings.stt_model)
        if use_deepgram
        else openai.STT(model="gpt-4o-mini-transcribe")
    )
    return AgentSession(
        vad=silero.VAD.load(),
        stt=stt_impl,
        llm=openai.LLM(model=settings.llm_model),
        tts=openai.TTS(model=settings.tts_model, voice=settings.tts_voice),
    )


def build_realtime_session() -> AgentSession:
    """Realtime speech-to-speech session (no standalone STT/TTS pipeline)."""
    return AgentSession(
        llm=openai.realtime.RealtimeModel(
            model=settings.realtime_model,
            voice=settings.realtime_voice,
            http_session=_get_realtime_http_session(),
        ),
    )
