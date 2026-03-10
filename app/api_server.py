from __future__ import annotations

import asyncio
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
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from livekit import api
from livekit.api import AccessToken, VideoGrants
from livekit.protocol.room import CreateRoomRequest, ListRoomsRequest, UpdateRoomMetadataRequest
from pydantic import BaseModel, Field
import certifi

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()

from shared.agent_dispatch import ensure_agent_for_room, prepare_observer_room
from app.positions_service import (
    create_position,
    delete_position,
    extract_position_details,
    extract_text_from_file,
    get_position,
    load_positions,
    update_position,
)
from app.candidates_service import (
    create_candidate,
    delete_candidate,
    extract_candidate_details,
    extract_candidate_from_file,
    get_candidate,
    load_candidates,
    update_candidate,
)
from app.applications_service import (
    build_interview,
    create_application,
    delete_application,
    get_application,
    load_applications,
    screen_application,
    update_application,
)
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
dispatch_reconcile_tasks: dict[str, asyncio.Task[None]] = {}
dispatch_room_locks: dict[str, asyncio.Lock] = {}
# Keep token issuance snappy even when AgentDispatchService is unhealthy.
DISPATCH_SYNC_TIMEOUT_SECONDS = 0.8


def _dispatch_room_lock(room: str) -> asyncio.Lock:
    room_key = room.strip().lower()
    lock = dispatch_room_locks.get(room_key)
    if lock is None:
        lock = asyncio.Lock()
        dispatch_room_locks[room_key] = lock
    return lock


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


class PositionUpsertRequest(BaseModel):
    role_title: str = ""
    jd_text: str = ""
    level: str = ""
    must_haves: list[str] = Field(default_factory=list)
    nice_to_haves: list[str] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    focus_areas: list[str] = Field(default_factory=list)
    evaluation_policy: str = ""
    extraction_confidence: dict[str, float] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)


class PositionRecord(PositionUpsertRequest):
    position_id: str
    created_by: str
    created_at: str
    updated_at: str
    version: int


class PositionExtractResponse(PositionUpsertRequest):
    used_llm: bool
    warnings: list[str] = Field(default_factory=list)


class CandidateCVMetadata(BaseModel):
    originalName: str = ""
    storedName: str = ""
    contentType: str = ""
    size: int = 0


class CandidateUpsertRequest(BaseModel):
    fullName: str = ""
    email: str = ""
    currentTitle: str = ""
    yearsExperience: float | None = None
    keySkills: list[str] = Field(default_factory=list)
    keyProjectHighlights: list[str] = Field(default_factory=list)
    candidateContext: str = ""
    cvTextSummary: str = ""
    cvMetadata: CandidateCVMetadata | None = None
    screeningCache: dict[str, Any] | None = None


class CandidateRecord(CandidateUpsertRequest):
    id: str
    createdAt: str
    updatedAt: str


class CandidateExtractResponse(CandidateUpsertRequest):
    used_llm: bool
    warnings: list[str] = Field(default_factory=list)


class ApplicationScreening(BaseModel):
    score: float | None = None
    overall_match_score: float | None = None
    justification: str = ""
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    job_requirements_summary: dict[str, Any] = Field(default_factory=dict)
    candidate_profile_summary: dict[str, Any] = Field(default_factory=dict)
    match_analysis: dict[str, Any] = Field(default_factory=dict)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    hiring_recommendation: str = ""
    hiring_reasoning: list[str] = Field(default_factory=list)
    interview_questions: list[str] = Field(default_factory=list)
    report: str = ""
    used_llm: bool = False
    updated_at: str = ""


class ApplicationInterview(BaseModel):
    room: str = ""
    scheduled_for: str | None = None
    stage: str = "technical_screen"
    status: str = "scheduled"
    agent: str = "interviewer"
    notes: str = ""
    updated_at: str = ""
    happened: bool = False
    transcript_available: bool = False
    transcript_line_count: int = 0


class ApplicationPositionSnapshot(BaseModel):
    position_id: str = ""
    role_title: str = ""
    level: str = ""
    must_haves: list[str] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)


class ApplicationCandidateSnapshot(BaseModel):
    candidate_id: str = ""
    fullName: str = ""
    email: str = ""
    currentTitle: str = ""
    yearsExperience: float | None = None
    keySkills: list[str] = Field(default_factory=list)


class ApplicationUpsertRequest(BaseModel):
    position_id: str = ""
    candidate_id: str = ""
    status: str = "applied"
    source: str = "manual"
    notes: str = ""
    screening: ApplicationScreening | None = None
    interview: ApplicationInterview | None = None
    interviews: list[ApplicationInterview] = Field(default_factory=list)
    position_snapshot: ApplicationPositionSnapshot | None = None
    candidate_snapshot: ApplicationCandidateSnapshot | None = None


class ApplicationRecord(ApplicationUpsertRequest):
    application_id: str
    created_by: str
    created_at: str
    updated_at: str
    version: int
    interview_happened: bool = False
    delete_allowed: bool = True


class ApplicationScreenResponse(BaseModel):
    application: ApplicationRecord
    used_llm: bool
    warnings: list[str] = Field(default_factory=list)


class ApplicationScreenPreviewRequest(BaseModel):
    position_id: str
    candidate_id: str


class ApplicationScreenPreviewResponse(BaseModel):
    screening: ApplicationScreening
    used_llm: bool
    warnings: list[str] = Field(default_factory=list)


class ScheduleInterviewRequest(BaseModel):
    scheduled_for: str | None = None
    stage: str | None = None
    agent: str = "interviewer"
    notes: str = ""


def _transcript_line_count_for_room(room: str) -> int:
    normalized_room = str(room or "").strip()
    if not normalized_room:
        return 0
    path = _transcript_file_path(normalized_room)
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as fp:
        for raw in fp:
            if raw.strip():
                count += 1
    return count


def _enrich_interview_runtime(interview: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(interview, dict):
        return None
    room = str(interview.get("room") or "").strip()
    if not room:
        return None
    line_count = _transcript_line_count_for_room(room)
    enriched = {**interview}
    enriched["transcript_line_count"] = line_count
    enriched["transcript_available"] = line_count > 0
    enriched["happened"] = line_count > 0
    return enriched


def _enrich_application_runtime(row: dict[str, Any]) -> dict[str, Any]:
    enriched = {**row}
    interviews = row.get("interviews") if isinstance(row.get("interviews"), list) else []
    enriched_interviews: list[dict[str, Any]] = []
    for item in interviews:
        runtime_item = _enrich_interview_runtime(item if isinstance(item, dict) else None)
        if runtime_item is not None:
            enriched_interviews.append(runtime_item)

    latest = _enrich_interview_runtime(row.get("interview") if isinstance(row.get("interview"), dict) else None)
    if latest is None and enriched_interviews:
        latest = sorted(enriched_interviews, key=lambda x: str(x.get("updated_at") or ""), reverse=True)[0]

    enriched["interview"] = latest
    enriched["interviews"] = enriched_interviews
    interview_happened = bool(latest and latest.get("happened")) or any(
        bool(item.get("happened")) for item in enriched_interviews
    )
    enriched["interview_happened"] = interview_happened
    enriched["delete_allowed"] = not interview_happened
    return enriched


def _build_room_metadata(*, agent: str, instructions: str | None = None) -> str:
    payload: dict[str, str] = {
        "agent": agent.strip().lower(),
        "room_mode": "human_ai",
    }
    if instructions:
        payload["instructions"] = instructions.strip()
    return json.dumps(payload)


async def ensure_room_metadata(*, room: str, agent: str, instructions: str | None = None) -> None:
    metadata = _build_room_metadata(agent=agent, instructions=instructions)
    async with api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    ) as lk:
        rooms = await lk.room.list_rooms(ListRoomsRequest(names=[room]))
        if not rooms.rooms:
            await lk.room.create_room(CreateRoomRequest(name=room, metadata=metadata))
            return

        existing = rooms.rooms[0]
        if (getattr(existing, "metadata", None) or "") == metadata:
            return
        await lk.room.update_room_metadata(UpdateRoomMetadataRequest(room=room, metadata=metadata))


def schedule_dispatch_reconcile(*, room: str, agent: str, instructions: str | None) -> None:
    existing = dispatch_reconcile_tasks.get(room)
    if existing is not None and not existing.done():
        return

    async def _runner() -> None:
        try:
            for attempt in range(30):
                try:
                    result = await ensure_agent_for_room(
                        room=room,
                        agent=agent,
                        instructions=instructions,
                    )
                    if result.created_dispatch or result.had_valid_dispatch or result.has_agent:
                        logging.info(
                            "dispatch_reconcile_success room=%s agent=%s attempt=%s created=%s had_dispatch=%s has_agent=%s",
                            room,
                            agent,
                            attempt + 1,
                            result.created_dispatch,
                            result.had_valid_dispatch,
                            result.has_agent,
                        )
                        return
                except Exception as exc:  # noqa: BLE001
                    if attempt in (0, 5, 10, 20, 29):
                        logging.warning(
                            "dispatch_reconcile_retry room=%s agent=%s attempt=%s error=%s",
                            room,
                            agent,
                            attempt + 1,
                            exc,
                        )
                await asyncio.sleep(0.5)
            logging.warning("dispatch_reconcile_giveup room=%s agent=%s", room, agent)
        finally:
            dispatch_reconcile_tasks.pop(room, None)

    dispatch_reconcile_tasks[room] = asyncio.create_task(_runner())


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
            elif payload.ai_enabled and not settings.explicit_dispatch:
                # Fallback mode when AgentDispatchService is unavailable:
                # store target agent on room metadata so auto-dispatched workers can route correctly.
                try:
                    await ensure_room_metadata(
                        room=room_name,
                        agent=selected_agent,
                        instructions=payload.instructions,
                    )
                except Exception as room_metadata_exc:  # noqa: BLE001
                    logging.warning(
                        "room_metadata_upsert_failed room=%s agent=%s error=%s",
                        room_name,
                        selected_agent,
                        room_metadata_exc,
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

            should_dispatch_agent = payload.ai_enabled and settings.explicit_dispatch and selected_agent != "observer"
            if should_dispatch_agent:
                dispatch_lock = _dispatch_room_lock(room_name)
                async with dispatch_lock:
                    metrics.dispatch_requests += 1
                    dispatch_result = None
                    dispatch_exc: Exception | None = None
                    # Serialize per-room dispatch attempts to avoid duplicate agent jobs
                    # when join submits happen near-simultaneously.
                    # Fail fast on dispatch health issues to avoid long /api/token latency.
                    try:
                        dispatch_result = await asyncio.wait_for(
                            ensure_agent_for_room(
                                room=room_name,
                                agent=payload.agent,
                                instructions=payload.instructions,
                            ),
                            timeout=DISPATCH_SYNC_TIMEOUT_SECONDS,
                        )
                        dispatch_exc = None
                    except Exception as exc:  # noqa: BLE001
                        dispatch_exc = exc

                    if dispatch_result and dispatch_result.created_dispatch:
                        metrics.dispatch_created += 1
                    logging.info(
                        "dispatch_check room=%s agent=%s room_exists=%s has_humans=%s has_agent=%s had_valid_dispatch=%s created_dispatch=%s",
                        room_name,
                        payload.agent,
                        dispatch_result.room_exists if dispatch_result else False,
                        dispatch_result.has_humans if dispatch_result else False,
                        dispatch_result.has_agent if dispatch_result else False,
                        dispatch_result.had_valid_dispatch if dispatch_result else False,
                        dispatch_result.created_dispatch if dispatch_result else False,
                    )

                    if (
                        not dispatch_result
                        or (
                            not dispatch_result.created_dispatch
                            and not dispatch_result.had_valid_dispatch
                            and not dispatch_result.has_agent
                        )
                    ):
                        if dispatch_exc:
                            logging.warning(
                                "dispatch_unavailable room=%s agent=%s error=%s",
                                room_name,
                                payload.agent,
                                dispatch_exc,
                            )
                        logging.warning(
                            "dispatch_not_confirmed room=%s agent=%s falling_back_to_room_metadata=1",
                            room_name,
                            payload.agent,
                        )
                        # Fallback when AgentDispatchService is unavailable or no dispatch
                        # could be confirmed: store target agent on room metadata so implicit
                        # jobs can route correctly.
                        try:
                            await ensure_room_metadata(
                                room=room_name,
                                agent=selected_agent,
                                instructions=payload.instructions,
                            )
                            logging.info(
                                "dispatch_fallback_room_metadata room=%s agent=%s",
                                room_name,
                                selected_agent,
                            )
                        except Exception as room_metadata_exc:  # noqa: BLE001
                            logging.warning(
                                "dispatch_fallback_room_metadata_failed room=%s agent=%s error=%s",
                                room_name,
                                selected_agent,
                                room_metadata_exc,
                            )
                        schedule_dispatch_reconcile(
                            room=room_name,
                            agent=payload.agent,
                            instructions=payload.instructions,
                        )

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

    @app.get("/api/positions", response_model=list[PositionRecord])
    async def list_positions(username: str = Depends(require_session_user)) -> list[PositionRecord]:
        _ = username
        return [PositionRecord(**row) for row in load_positions()]

    @app.get("/api/positions/{position_id}", response_model=PositionRecord)
    async def get_position_by_id(
        position_id: str,
        username: str = Depends(require_session_user),
    ) -> PositionRecord:
        _ = username
        row = get_position(position_id)
        if row is None:
            raise HTTPException(status_code=404, detail="position not found")
        return PositionRecord(**row)

    @app.post("/api/positions", response_model=PositionRecord)
    async def create_new_position(
        payload: PositionUpsertRequest,
        username: str = Depends(require_session_user),
    ) -> PositionRecord:
        created = create_position(payload.model_dump(), created_by=username)
        return PositionRecord(**created)

    @app.put("/api/positions/{position_id}", response_model=PositionRecord)
    async def update_existing_position(
        position_id: str,
        payload: PositionUpsertRequest,
        username: str = Depends(require_session_user),
    ) -> PositionRecord:
        _ = username
        updated = update_position(position_id, payload.model_dump())
        if updated is None:
            raise HTTPException(status_code=404, detail="position not found")
        return PositionRecord(**updated)

    @app.delete("/api/positions/{position_id}")
    async def delete_existing_position(
        position_id: str,
        username: str = Depends(require_session_user),
    ) -> dict[str, bool]:
        _ = username
        deleted = delete_position(position_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="position not found")
        return {"ok": True}

    @app.post("/api/positions/extract", response_model=PositionExtractResponse)
    async def extract_position_payload(
        jd_text: str | None = Form(default=None),
        file: UploadFile | None = File(default=None),
        username: str = Depends(require_session_user),
    ) -> PositionExtractResponse:
        _ = username
        jd_parts: list[str] = []

        if file is not None:
            raw = await file.read()
            if not raw:
                raise HTTPException(status_code=400, detail="uploaded file is empty")
            try:
                extracted_text = extract_text_from_file(file.filename or "upload.txt", raw)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"unable to parse uploaded file: {exc}") from exc

            if extracted_text.strip():
                jd_parts.append(extracted_text.strip())

        if jd_text and jd_text.strip():
            jd_parts.append(jd_text.strip())

        if not jd_parts:
            raise HTTPException(status_code=400, detail="provide jd_text or a valid file")

        merged_jd_text = "\n\n".join(jd_parts).strip()
        extracted, used_llm, warnings = extract_position_details(merged_jd_text)
        return PositionExtractResponse(**extracted, used_llm=used_llm, warnings=warnings)

    @app.get("/api/candidates", response_model=list[CandidateRecord])
    async def list_all_candidates(username: str = Depends(require_session_user)) -> list[CandidateRecord]:
        _ = username
        return [CandidateRecord(**row) for row in load_candidates()]

    @app.get("/api/candidates/{candidate_id}", response_model=CandidateRecord)
    async def get_candidate_by_id(
        candidate_id: str,
        username: str = Depends(require_session_user),
    ) -> CandidateRecord:
        _ = username
        row = get_candidate(candidate_id)
        if row is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        return CandidateRecord(**row)

    @app.post("/api/candidates", response_model=CandidateRecord)
    async def create_new_candidate(
        payload: CandidateUpsertRequest,
        username: str = Depends(require_session_user),
    ) -> CandidateRecord:
        _ = username
        created = create_candidate(payload.model_dump())
        return CandidateRecord(**created)

    @app.put("/api/candidates/{candidate_id}", response_model=CandidateRecord)
    async def update_existing_candidate(
        candidate_id: str,
        payload: CandidateUpsertRequest,
        username: str = Depends(require_session_user),
    ) -> CandidateRecord:
        _ = username
        updated = update_candidate(candidate_id, payload.model_dump())
        if updated is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        return CandidateRecord(**updated)

    @app.delete("/api/candidates/{candidate_id}")
    async def delete_existing_candidate(
        candidate_id: str,
        username: str = Depends(require_session_user),
    ) -> dict[str, bool]:
        _ = username
        deleted = delete_candidate(candidate_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="candidate not found")
        return {"ok": True}

    @app.post("/api/candidates/extract", response_model=CandidateExtractResponse)
    async def extract_candidate_payload(
        cv_text: str | None = Form(default=None),
        file: UploadFile | None = File(default=None),
        username: str = Depends(require_session_user),
    ) -> CandidateExtractResponse:
        _ = username
        text_parts: list[str] = []
        cv_metadata: dict[str, Any] | None = None

        if file is not None:
            raw = await file.read()
            if not raw:
                raise HTTPException(status_code=400, detail="uploaded file is empty")
            try:
                extracted_text, cv_metadata = extract_candidate_from_file(file.filename or "candidate.txt", raw)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"unable to parse uploaded file: {exc}") from exc
            if extracted_text.strip():
                text_parts.append(extracted_text.strip())

        if cv_text and cv_text.strip():
            text_parts.append(cv_text.strip())

        if not text_parts:
            raise HTTPException(status_code=400, detail="provide cv_text or a valid file")

        merged_text = "\n\n".join(text_parts).strip()
        extracted, used_llm, warnings = extract_candidate_details(merged_text)
        payload = {**extracted, "cvMetadata": cv_metadata}
        return CandidateExtractResponse(**payload, used_llm=used_llm, warnings=warnings)

    @app.get("/api/applications", response_model=list[ApplicationRecord])
    async def list_all_applications(username: str = Depends(require_session_user)) -> list[ApplicationRecord]:
        _ = username
        return [ApplicationRecord(**_enrich_application_runtime(row)) for row in load_applications()]

    @app.get("/api/applications/{application_id}", response_model=ApplicationRecord)
    async def get_application_by_id(
        application_id: str,
        username: str = Depends(require_session_user),
    ) -> ApplicationRecord:
        _ = username
        row = get_application(application_id)
        if row is None:
            raise HTTPException(status_code=404, detail="application not found")
        return ApplicationRecord(**_enrich_application_runtime(row))

    @app.post("/api/applications", response_model=ApplicationRecord)
    async def create_new_application(
        payload: ApplicationUpsertRequest,
        username: str = Depends(require_session_user),
    ) -> ApplicationRecord:
        body = payload.model_dump()
        position_id = str(body.get("position_id") or "").strip()
        candidate_id = str(body.get("candidate_id") or "").strip()
        if not position_id or not candidate_id:
            raise HTTPException(status_code=400, detail="position_id and candidate_id are required")

        position = get_position(position_id)
        if position is None:
            raise HTTPException(status_code=404, detail="position not found")
        candidate = get_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        if body.get("screening") is None:
            raise HTTPException(status_code=400, detail="run screening before creating application")

        try:
            created = create_application(body, created_by=username, position=position, candidate=candidate)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ApplicationRecord(**_enrich_application_runtime(created))

    @app.put("/api/applications/{application_id}", response_model=ApplicationRecord)
    async def update_existing_application(
        application_id: str,
        payload: ApplicationUpsertRequest,
        username: str = Depends(require_session_user),
    ) -> ApplicationRecord:
        _ = username
        body = payload.model_dump()
        position_id = str(body.get("position_id") or "").strip()
        candidate_id = str(body.get("candidate_id") or "").strip()
        if not position_id or not candidate_id:
            raise HTTPException(status_code=400, detail="position_id and candidate_id are required")

        position = get_position(position_id)
        if position is None:
            raise HTTPException(status_code=404, detail="position not found")
        candidate = get_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="candidate not found")

        updated = update_application(application_id, body, position=position, candidate=candidate)
        if updated is None:
            raise HTTPException(status_code=404, detail="application not found")
        return ApplicationRecord(**_enrich_application_runtime(updated))

    @app.delete("/api/applications/{application_id}")
    async def delete_existing_application(
        application_id: str,
        username: str = Depends(require_session_user),
    ) -> dict[str, bool]:
        _ = username
        existing = get_application(application_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="application not found")
        if _enrich_application_runtime(existing).get("interview_happened"):
            raise HTTPException(status_code=409, detail="cannot delete application after interview has happened")
        deleted = delete_application(application_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="application not found")
        return {"ok": True}

    @app.post("/api/applications/{application_id}/screen", response_model=ApplicationScreenResponse)
    async def screen_existing_application(
        application_id: str,
        username: str = Depends(require_session_user),
    ) -> ApplicationScreenResponse:
        _ = username
        existing = get_application(application_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="application not found")

        position = get_position(existing.get("position_id") or "")
        if position is None:
            raise HTTPException(status_code=404, detail="linked position not found")
        candidate = get_candidate(existing.get("candidate_id") or "")
        if candidate is None:
            raise HTTPException(status_code=404, detail="linked candidate not found")

        screening, used_llm, warnings = screen_application(position, candidate)
        next_payload = {
            **existing,
            "screening": screening,
            "status": existing.get("status") if existing.get("status") == "interview_scheduled" else "screened",
        }
        updated = update_application(application_id, next_payload, position=position, candidate=candidate)
        if updated is None:
            raise HTTPException(status_code=404, detail="application not found")

        return ApplicationScreenResponse(
            application=ApplicationRecord(**_enrich_application_runtime(updated)),
            used_llm=used_llm,
            warnings=warnings,
        )

    @app.post("/api/applications/screen-preview", response_model=ApplicationScreenPreviewResponse)
    async def screen_application_preview(
        payload: ApplicationScreenPreviewRequest,
        username: str = Depends(require_session_user),
    ) -> ApplicationScreenPreviewResponse:
        _ = username
        position = get_position(payload.position_id)
        if position is None:
            raise HTTPException(status_code=404, detail="position not found")
        candidate = get_candidate(payload.candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="candidate not found")

        screening, used_llm, warnings = screen_application(position, candidate)
        return ApplicationScreenPreviewResponse(
            screening=ApplicationScreening(**screening),
            used_llm=used_llm,
            warnings=warnings,
        )

    @app.post("/api/applications/{application_id}/schedule-interview", response_model=ApplicationRecord)
    async def schedule_application_interview(
        application_id: str,
        payload: ScheduleInterviewRequest,
        username: str = Depends(require_session_user),
    ) -> ApplicationRecord:
        _ = username
        existing = get_application(application_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="application not found")

        position = get_position(existing.get("position_id") or "")
        if position is None:
            raise HTTPException(status_code=404, detail="linked position not found")
        candidate = get_candidate(existing.get("candidate_id") or "")
        if candidate is None:
            raise HTTPException(status_code=404, detail="linked candidate not found")

        interview = build_interview(
            application_id=application_id,
            position_title=str(position.get("role_title") or ""),
            candidate_name=str(candidate.get("fullName") or ""),
            scheduled_for=payload.scheduled_for,
            stage=payload.stage,
            agent=payload.agent,
            notes=payload.notes,
        )
        prior_interviews = existing.get("interviews") if isinstance(existing.get("interviews"), list) else []
        merged_interviews = [
            item
            for item in prior_interviews
            if isinstance(item, dict) and str(item.get("room") or "").strip()
        ]
        merged_interviews.append(interview)
        next_payload = {
            **existing,
            "interview": interview,
            "interviews": merged_interviews,
            "status": "interview_scheduled",
        }
        updated = update_application(application_id, next_payload, position=position, candidate=candidate)
        if updated is None:
            raise HTTPException(status_code=404, detail="application not found")
        return ApplicationRecord(**_enrich_application_runtime(updated))

    @app.get("/api/interviews", response_model=list[ApplicationRecord])
    async def list_scheduled_interviews(
        username: str = Depends(require_session_user),
    ) -> list[ApplicationRecord]:
        _ = username
        rows = [row for row in load_applications() if isinstance(row.get("interview"), dict) and row["interview"].get("room")]
        return [ApplicationRecord(**_enrich_application_runtime(row)) for row in rows]

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
