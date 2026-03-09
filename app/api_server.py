from __future__ import annotations

import logging
import json
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from livekit.api import AccessToken, VideoGrants
from pydantic import BaseModel, Field
import certifi

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()

from shared.agent_dispatch import ensure_agent_for_room, prepare_observer_room
from shared.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass
class Metrics:
    started_at: float
    login_success: int = 0
    login_failed: int = 0
    token_requests: int = 0
    token_failures: int = 0
    dispatch_requests: int = 0
    dispatch_created: int = 0
    join_failures: int = 0
    media_permission_failures: int = 0


metrics = Metrics(started_at=time.time())
transcript_lock = threading.Lock()
transcript_seen_keys: dict[str, set[str]] = {}


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    username: str


class MeResponse(BaseModel):
    username: str


class Capabilities(BaseModel):
    can_publish: bool = True
    can_subscribe: bool = True
    can_publish_data: bool = True
    can_publish_sources: list[str] = Field(default_factory=lambda: ["microphone", "camera", "screen_share"])


class TokenRequest(BaseModel):
    room: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    ai_enabled: bool = True
    agent: str = Field(default_factory=lambda: settings.default_agent)
    instructions: str | None = Field(default=None, max_length=1000)


class TokenResponse(BaseModel):
    token: str
    server_url: str
    identity: str
    room: str
    expires_at: str


class ClientEventRequest(BaseModel):
    event: str
    detail: str | None = None


class OpenAIRealtimeTokenRequest(BaseModel):
    model: str | None = None
    voice: str | None = None
    instructions: str | None = Field(default=None, max_length=2000)


class OpenAIRealtimeTokenResponse(BaseModel):
    client_secret: str
    model: str
    voice: str


class TranscriptAppendRequest(BaseModel):
    room: str = Field(min_length=1, max_length=128)
    speaker: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=8000)
    source: str = Field(default="livekit", max_length=64)
    unique_key: str | None = Field(default=None, max_length=256)
    timestamp: str | None = Field(default=None, max_length=64)


class TranscriptAppendResponse(BaseModel):
    ok: bool
    appended: bool


class TranscriptStatusResponse(BaseModel):
    room: str
    exists: bool
    line_count: int


def create_app() -> FastAPI:
    app = FastAPI(title="LiveKit Meeting API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/auth/login", response_model=LoginResponse)
    async def login(payload: LoginRequest, response: Response) -> LoginResponse:
        if payload.username != settings.app_auth_user or payload.password != settings.app_auth_password:
            metrics.login_failed += 1
            raise HTTPException(status_code=401, detail="invalid credentials")

        token = _encode_session(payload.username)
        response.set_cookie(
            key=settings.session_cookie_name,
            value=token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=settings.session_ttl_seconds,
        )
        metrics.login_success += 1
        return LoginResponse(username=payload.username)

    @app.post("/api/auth/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(key=settings.session_cookie_name)
        return {"ok": True}

    @app.get("/api/auth/me", response_model=MeResponse)
    async def me(username: str = Depends(require_session_user)) -> MeResponse:
        return MeResponse(username=username)

    @app.post("/api/token", response_model=TokenResponse)
    async def create_token(payload: TokenRequest, username: str = Depends(require_session_user)) -> TokenResponse:
        metrics.token_requests += 1

        try:
            selected_agent = payload.agent.strip().lower()
            requested_room = payload.room.strip()
            if not requested_room:
                raise HTTPException(status_code=400, detail="room is required")

            room_name = requested_room
            observer_suffix = "-observer"
            if selected_agent == "observer" and not room_name.endswith(observer_suffix):
                room_name = f"{room_name}{observer_suffix}"

            # Enforce room-mode isolation at server boundary even if client is stale.
            if selected_agent != "observer" and room_name.endswith(observer_suffix):
                raise HTTPException(
                    status_code=409,
                    detail="observer rooms are reserved for observer agent only",
                )

            if selected_agent == "observer":
                prep = await prepare_observer_room(room_name)
                if prep.blocked_by_humans:
                    humans = ", ".join(prep.human_identities) or "active participants"
                    raise HTTPException(
                        status_code=409,
                        detail=f'observer mode requires an empty room. Active participants found: {humans}',
                    )
                if prep.removed_dispatches or prep.removed_agents:
                    logging.info(
                        "observer_room_prepared room=%s removed_dispatches=%s removed_agents=%s",
                        room_name,
                        prep.removed_dispatches,
                        prep.removed_agents,
                    )

            identity = f"{username}-{secrets.token_hex(4)}"
            now = datetime.now(UTC)
            expires_at = now + timedelta(seconds=settings.token_ttl_seconds)

            token = (
                AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
                .with_identity(identity)
                .with_name(payload.display_name or username)
                .with_ttl(timedelta(seconds=settings.token_ttl_seconds))
                .with_grants(
                    VideoGrants(
                        room_join=True,
                        room=room_name,
                        can_subscribe=payload.capabilities.can_subscribe,
                        can_publish=payload.capabilities.can_publish,
                        can_publish_data=payload.capabilities.can_publish_data,
                        can_publish_sources=payload.capabilities.can_publish_sources,
                    )
                )
            ).to_jwt()

            should_dispatch_agent = payload.ai_enabled and selected_agent != "observer"
            if should_dispatch_agent:
                metrics.dispatch_requests += 1
                result = await ensure_agent_for_room(
                    room=room_name,
                    agent=payload.agent,
                    instructions=payload.instructions,
                )
                if result.created_dispatch:
                    metrics.dispatch_created += 1

            logging.info(
                "issued token user=%s identity=%s room=%s ai_enabled=%s dispatch_enabled=%s agent=%s",
                username,
                identity,
                room_name,
                payload.ai_enabled,
                should_dispatch_agent,
                payload.agent,
            )

            return TokenResponse(
                token=token,
                server_url=settings.livekit_url,
                identity=identity,
                room=room_name,
                expires_at=expires_at.isoformat(),
            )
        except HTTPException:
            metrics.token_failures += 1
            raise
        except Exception as exc:  # noqa: BLE001
            metrics.token_failures += 1
            logging.exception("token issuance failed user=%s room=%s", username, payload.room)
            raise HTTPException(status_code=500, detail=f"token issuance failed: {exc}") from exc

    @app.post("/api/client-event")
    async def client_event(
        payload: ClientEventRequest,
        username: str = Depends(require_session_user),
    ) -> dict[str, bool]:
        if payload.event == "join_failure":
            metrics.join_failures += 1
        if payload.event == "media_permission_failure":
            metrics.media_permission_failures += 1

        logging.warning(
            "client_event user=%s event=%s detail=%s",
            username,
            payload.event,
            payload.detail,
        )
        return {"ok": True}

    @app.post("/api/openai/realtime/token", response_model=OpenAIRealtimeTokenResponse)
    async def create_openai_realtime_token(
        payload: OpenAIRealtimeTokenRequest,
        username: str = Depends(require_session_user),
    ) -> OpenAIRealtimeTokenResponse:
        if not settings.openai_api_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not configured")

        model = (payload.model or settings.realtime_model).strip()
        voice = (payload.voice or settings.realtime_voice).strip()

        session: dict[str, object] = {
            "type": "realtime",
            "model": model,
            "audio": {
                "input": {
                    "transcription": {
                        "model": "gpt-4o-mini-transcribe",
                    }
                },
                "output": {
                    "voice": voice,
                }
            },
        }
        if payload.instructions:
            session["instructions"] = payload.instructions.strip()

        session_payload: dict[str, object] = {"session": session}

        req = urllib.request.Request(
            "https://api.openai.com/v1/realtime/client_secrets",
            data=json.dumps(session_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(req, timeout=20, context=ssl_ctx) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            secret = (
                body.get("value")
                or body.get("client_secret", {}).get("value")
                or body.get("secret")
            )
            if not isinstance(secret, str) or not secret:
                raise HTTPException(status_code=502, detail="OpenAI realtime token missing in response")

            logging.info("issued openai realtime token user=%s model=%s voice=%s", username, model, voice)
            return OpenAIRealtimeTokenResponse(
                client_secret=secret,
                model=model,
                voice=voice,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            logging.exception("openai realtime token failed user=%s", username)
            raise HTTPException(
                status_code=502,
                detail=f"OpenAI realtime token request failed: {exc.code} {detail[:300]}",
            ) from exc
        except urllib.error.URLError as exc:
            logging.exception("openai realtime token network error user=%s", username)
            raise HTTPException(status_code=502, detail=f"OpenAI realtime token network error: {exc.reason}") from exc

    @app.post("/api/transcripts/append", response_model=TranscriptAppendResponse)
    async def append_transcript(
        payload: TranscriptAppendRequest,
        username: str = Depends(require_session_user),
    ) -> TranscriptAppendResponse:
        room = payload.room.strip()
        speaker = payload.speaker.strip()
        text = payload.text.strip()
        if not room or not speaker or not text:
            return TranscriptAppendResponse(ok=True, appended=False)

        line = {
            "timestamp": payload.timestamp or datetime.now(UTC).isoformat(),
            "room": room,
            "speaker": speaker,
            "source": payload.source.strip() or "livekit",
            "text": text,
            "username": username,
        }

        with transcript_lock:
            unique_key = (payload.unique_key or "").strip()
            if unique_key:
                room_seen = transcript_seen_keys.setdefault(room, set())
                if unique_key in room_seen:
                    return TranscriptAppendResponse(ok=True, appended=False)
                room_seen.add(unique_key)

            path = _transcript_file_path(room)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(line, ensure_ascii=False) + "\n")

        return TranscriptAppendResponse(ok=True, appended=True)

    @app.get("/api/transcripts/{room}/status", response_model=TranscriptStatusResponse)
    async def transcript_status(
        room: str,
        username: str = Depends(require_session_user),
    ) -> TranscriptStatusResponse:
        _ = username
        normalized_room = room.strip()
        path = _transcript_file_path(normalized_room)
        if not path.exists():
            return TranscriptStatusResponse(room=normalized_room, exists=False, line_count=0)

        count = 0
        with path.open("r", encoding="utf-8") as fp:
            for raw in fp:
                if raw.strip():
                    count += 1
        return TranscriptStatusResponse(room=normalized_room, exists=count > 0, line_count=count)

    @app.get("/api/transcripts/{room}/download")
    async def download_transcript(
        room: str,
        username: str = Depends(require_session_user),
    ) -> PlainTextResponse:
        _ = username
        normalized_room = room.strip()
        path = _transcript_file_path(normalized_room)
        if not path.exists():
            raise HTTPException(status_code=404, detail="transcript not found")

        lines: list[str] = []
        with path.open("r", encoding="utf-8") as fp:
            for raw in fp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                timestamp = str(row.get("timestamp") or "")
                speaker = str(row.get("speaker") or "Unknown")
                source = str(row.get("source") or "livekit")
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                lines.append(f"[{timestamp}] {speaker} ({source}): {text}")

        if not lines:
            raise HTTPException(status_code=404, detail="transcript not found")

        body = "\n".join(lines) + "\n"
        safe_name = _safe_filename_component(normalized_room)
        return PlainTextResponse(
            content=body,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}-transcript.txt"'},
        )

    @app.get("/api/metrics")
    async def get_metrics(
        credentials: HTTPAuthorizationCredentials | None = Depends(optional_bearer),
    ) -> JSONResponse:
        if settings.metrics_bearer_token:
            if credentials is None or credentials.credentials != settings.metrics_bearer_token:
                raise HTTPException(status_code=401, detail="missing or invalid metrics token")

        return JSONResponse(
            {
                "uptime_seconds": int(time.time() - metrics.started_at),
                "login_success": metrics.login_success,
                "login_failed": metrics.login_failed,
                "token_requests": metrics.token_requests,
                "token_failures": metrics.token_failures,
                "dispatch_requests": metrics.dispatch_requests,
                "dispatch_created": metrics.dispatch_created,
                "join_failures": metrics.join_failures,
                "media_permission_failures": metrics.media_permission_failures,
            }
        )

    return app


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app_session_secret, salt="livekit-session")


def _encode_session(username: str) -> str:
    return _serializer().dumps({"u": username})


def _decode_session(token: str) -> str:
    try:
        data = _serializer().loads(token, max_age=settings.session_ttl_seconds)
        username = data.get("u")
        if not isinstance(username, str) or not username:
            raise HTTPException(status_code=401, detail="invalid session")
        return username
    except SignatureExpired as exc:
        raise HTTPException(status_code=401, detail="session expired") from exc
    except BadSignature as exc:
        raise HTTPException(status_code=401, detail="invalid session") from exc


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned[:128] or "room"


def _transcript_file_path(room: str) -> Path:
    safe_room = _safe_filename_component(room)
    return Path(__file__).resolve().parents[1] / "data" / "transcripts" / f"{safe_room}.jsonl"


def require_session_user(request: Request) -> str:
    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        raise HTTPException(status_code=401, detail="not authenticated")
    return _decode_session(cookie)


optional_bearer = HTTPBearer(auto_error=False)


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.api_server:app", host=settings.api_host, port=settings.api_port, reload=False)
