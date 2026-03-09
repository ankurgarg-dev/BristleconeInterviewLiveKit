from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    livekit_url: str = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
    livekit_api_key: str = os.getenv("LIVEKIT_API_KEY", "devkey")
    livekit_api_secret: str = os.getenv("LIVEKIT_API_SECRET", "secret")

    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "")

    default_agent: str = os.getenv("DEFAULT_AGENT", "assistant")
    dispatch_agent_name: str = os.getenv("DISPATCH_AGENT_NAME", "router")
    explicit_dispatch: bool = os.getenv("EXPLICIT_DISPATCH", "true").lower() == "true"

    stt_model: str = os.getenv("DEEPGRAM_STT_MODEL", "nova-3")
    llm_model: str = os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini")
    tts_model: str = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    tts_voice: str = os.getenv("OPENAI_TTS_VOICE", "alloy")
    realtime_model: str = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-mini")
    realtime_voice: str = os.getenv("OPENAI_REALTIME_VOICE", "alloy")

    # API server settings
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("API_PORT", "8080"))
    api_cors_origins: list[str] = field(
        default_factory=lambda: _csv_list(os.getenv("API_CORS_ORIGINS", "http://localhost:5173"))
    )

    app_auth_user: str = os.getenv("APP_AUTH_USER", "demo")
    app_auth_password: str = os.getenv("APP_AUTH_PASSWORD", "demo-pass")
    app_session_secret: str = os.getenv("APP_SESSION_SECRET", "change-me-before-production")
    session_cookie_name: str = os.getenv("SESSION_COOKIE_NAME", "lk_session")
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "43200"))
    token_ttl_seconds: int = int(os.getenv("TOKEN_TTL_SECONDS", "3600"))
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"

    metrics_bearer_token: str = os.getenv("METRICS_BEARER_TOKEN", "")


settings = Settings()
