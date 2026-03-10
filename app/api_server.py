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
import urllib.parse
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
from livekit.protocol.egress import (
    EgressInfo,
    EgressStatus,
    EncodedFileOutput,
    EncodedFileType,
    ListEgressRequest,
    RoomCompositeEgressRequest,
    StopEgressRequest,
)
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
from app.agent_prompts_service import get_effective_prompt, list_prompt_records, reset_prompt, set_prompt
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
recording_finalize_tasks: dict[str, asyncio.Task[None]] = {}
# Keep token issuance snappy even when AgentDispatchService is unhealthy.
DISPATCH_SYNC_TIMEOUT_SECONDS = 0.8
INTERVIEW_CONTEXT_MARKERS = ("$${INTERVIEW-CONTEXT}$$", "$${INTERVIEW_CONTEXT}$$")
INTERVIEW_CONTEXT_TEXT_CAP = 1500


def _dispatch_room_lock(room: str) -> asyncio.Lock:
    room_key = room.strip().lower()
    lock = dispatch_room_locks.get(room_key)
    if lock is None:
        lock = asyncio.Lock()
        dispatch_room_locks[room_key] = lock
    return lock


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _as_list_text(value: Any) -> str:
    if isinstance(value, list):
        parts = [_as_text(item) for item in value]
        parts = [part for part in parts if part]
        return ", ".join(parts)
    return _as_text(value)


def _pick_nonempty(*values: Any) -> str:
    for value in values:
        text = _as_text(value)
        if text:
            return text
    return ""


def _default_if_empty(value: str, default: str = "Not available") -> str:
    text = _as_text(value)
    return text or default


def _truncate_context_text(value: str, max_chars: int = INTERVIEW_CONTEXT_TEXT_CAP) -> str:
    text = _as_text(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "... [truncated]"


def _build_jd_summary(position: dict[str, Any], screening: dict[str, Any], application: dict[str, Any]) -> str:
    role = _pick_nonempty(position.get("role_title"), application.get("position_snapshot", {}).get("role_title"))
    level = _pick_nonempty(position.get("level"), application.get("position_snapshot", {}).get("level"))
    focus = _as_list_text(position.get("focus_areas"))
    tech = _as_list_text(position.get("tech_stack"))
    llm_summary = _pick_nonempty(
        screening.get("job_requirements_summary", {}).get("summary"),
        screening.get("justification"),
    )
    if llm_summary:
        return llm_summary
    parts = [part for part in [role, level, focus, tech] if _as_text(part)]
    return " | ".join(parts)


def _find_application_by_interview_room(room: str) -> dict[str, Any] | None:
    target = _as_text(room)
    if not target:
        return None
    for row in load_applications():
        if not isinstance(row, dict):
            continue
        current = row.get("interview")
        if isinstance(current, dict) and _as_text(current.get("room")) == target:
            return row
        attempts = row.get("interviews")
        if isinstance(attempts, list):
            for item in attempts:
                if isinstance(item, dict) and _as_text(item.get("room")) == target:
                    return row
    return None


def _interview_entry_for_room(application: dict[str, Any], room: str) -> dict[str, Any]:
    target = _as_text(room)
    current = application.get("interview")
    if isinstance(current, dict) and _as_text(current.get("room")) == target:
        return current
    attempts = application.get("interviews")
    if isinstance(attempts, list):
        for item in attempts:
            if isinstance(item, dict) and _as_text(item.get("room")) == target:
                return item
    return current if isinstance(current, dict) else {}


def _build_interview_context_block(*, room: str) -> str | None:
    application = _find_application_by_interview_room(room)
    if not application:
        return None

    interview = _interview_entry_for_room(application, room)
    position = get_position(_as_text(application.get("position_id"))) or {}
    candidate = get_candidate(_as_text(application.get("candidate_id"))) or {}
    screening = application.get("screening") if isinstance(application.get("screening"), dict) else {}

    jd_text = _pick_nonempty(position.get("jd_text"))
    jd_summary = _build_jd_summary(position, screening, application)
    platform = _as_list_text(position.get("tech_stack"))
    must_have = _pick_nonempty(
        _as_list_text(position.get("must_haves")),
        _as_list_text(application.get("position_snapshot", {}).get("must_haves")),
    )
    nice_to_have = _as_list_text(position.get("nice_to_haves"))
    cv_text = _pick_nonempty(
        candidate.get("cvTextSummary"),
        candidate.get("candidateContext"),
        _as_list_text(candidate.get("keyProjectHighlights")),
    )
    duration = interview.get("duration_minutes") if isinstance(interview, dict) else None
    duration_text = f"{int(duration)} minutes" if isinstance(duration, (int, float)) else "30 minutes"

    return (
        "--------------------------------------------------\n"
        "INTERVIEW INPUT CONTEXT\n"
        "--------------------------------------------------\n\n"
        f"Job Description:\n{_default_if_empty(_truncate_context_text(jd_text))}\n\n"
        f"JD Summary:\n{_default_if_empty(jd_summary)}\n\n"
        f"Technology Platform:\n{_default_if_empty(platform)}\n\n"
        f"Must Have Skills:\n{_default_if_empty(must_have)}\n\n"
        f"Nice To Have Skills:\n{_default_if_empty(nice_to_have)}\n\n"
        f"Candidate CV:\n{_default_if_empty(_truncate_context_text(cv_text))}\n\n"
        f"Total Interview Duration:\n{duration_text}"
    )


def _expand_interview_context_placeholders(prompt: str, room: str) -> str:
    text = _as_text(prompt)
    if not text:
        return text
    marker_present = any(marker in text for marker in INTERVIEW_CONTEXT_MARKERS)
    if not marker_present:
        return text
    context_block = _build_interview_context_block(room=room)
    if not context_block:
        return text
    for marker in INTERVIEW_CONTEXT_MARKERS:
        text = text.replace(marker, context_block)
    return text


def _inject_prompt_trace_at_transcript_start(*, room: str, prompt: str, username: str) -> None:
    room_name = _as_text(room)
    prompt_text = _as_text(prompt)
    if not room_name or not prompt_text:
        return

    path = _transcript_file_path(room_name)
    line = {
        "timestamp": datetime.now(UTC).isoformat(),
        "room": room_name,
        "speaker": "System",
        "source": "system-prompt",
        "text": f"Resolved AI prompt for session:\n{prompt_text}",
        "username": username,
    }

    with transcript_lock:
        existing = ""
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            for raw in existing.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if str(row.get("source") or "").strip() == "system-prompt":
                    return

        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = json.dumps(line, ensure_ascii=False) + "\n"
        path.write_text(prefix + existing, encoding="utf-8")


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
    instructions: str = ""


class AgentPromptRecord(BaseModel):
    agent: str
    prompt: str
    default_prompt: str
    is_default: bool


class AgentPromptListResponse(BaseModel):
    prompts: list[AgentPromptRecord]


class AgentPromptUpdateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)


class ClientEventRequest(BaseModel):
    event: str
    detail: str | None = None


class OpenAIRealtimeTokenRequest(BaseModel):
    model: str | None = None
    voice: str | None = None
    instructions: str | None = Field(default=None, max_length=12000)


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
    duration_minutes: int = 30
    notes: str = ""
    updated_at: str = ""
    happened: bool = False
    transcript_available: bool = False
    transcript_line_count: int = 0
    recording_available: bool = False
    recording_size_bytes: int = 0
    recording_filename: str = ""
    recording_updated_at: str | None = None


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
    duration_minutes: int = Field(default=30, ge=1, le=90)
    notes: str = ""


class RecordingControlRequest(BaseModel):
    room: str


class RecordingStatusResponse(BaseModel):
    room: str
    is_recording: bool
    egress_id: str = ""
    egress_status: str = ""
    recording_available: bool = False
    recording_filename: str = ""
    recording_size_bytes: int = 0
    recording_updated_at: str | None = None


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


def _recordings_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "recordings"


def _is_egress_live(status: int) -> bool:
    return status in (
        EgressStatus.EGRESS_STARTING,
        EgressStatus.EGRESS_ACTIVE,
        EgressStatus.EGRESS_ENDING,
    )


def _is_egress_terminal(status: int) -> bool:
    return status in (
        EgressStatus.EGRESS_COMPLETE,
        EgressStatus.EGRESS_FAILED,
        EgressStatus.EGRESS_ABORTED,
        EgressStatus.EGRESS_LIMIT_REACHED,
    )


def _egress_status_name(status: int) -> str:
    try:
        return EgressStatus.Name(status)
    except Exception:  # noqa: BLE001
        return str(status)


def _egress_sort_key(info: EgressInfo) -> int:
    return int(getattr(info, "updated_at", 0) or getattr(info, "ended_at", 0) or getattr(info, "started_at", 0) or 0)


def _to_utc_datetime_from_epoch(value: int) -> datetime:
    raw = int(value or 0)
    if raw <= 0:
        return datetime.now(UTC)
    # Egress fields can be seconds, milliseconds, microseconds, or nanoseconds.
    if raw >= 1_000_000_000_000_000_000:
        return datetime.fromtimestamp(raw / 1_000_000_000, tz=UTC)
    if raw >= 1_000_000_000_000_000:
        return datetime.fromtimestamp(raw / 1_000_000, tz=UTC)
    if raw >= 1_000_000_000_000:
        return datetime.fromtimestamp(raw / 1_000, tz=UTC)
    return datetime.fromtimestamp(raw, tz=UTC)


def _recording_filename_for_egress(room: str, info: EgressInfo, ext: str) -> str:
    safe_room = _safe_filename_component(room)
    safe_egress_id = _safe_filename_component(getattr(info, "egress_id", "") or "egress")
    started_at_ms = int(getattr(info, "started_at", 0) or 0)
    stamp = _to_utc_datetime_from_epoch(started_at_ms).strftime("%Y%m%d%H%M%S")
    suffix = ext if ext.startswith(".") else f".{ext}"
    return f"{safe_room}__{stamp}__{safe_egress_id}{suffix}"


def _recording_extension_from_egress(info: EgressInfo) -> str:
    allowed = {".mp4", ".webm", ".ogg", ".mp3", ".m4a", ".wav"}
    for row in getattr(info, "file_results", []) or []:
        filename = str(getattr(row, "filename", "") or "").strip()
        if filename:
            suffix = Path(filename).suffix.lower()
            if suffix in allowed:
                return suffix
        location = str(getattr(row, "location", "") or "").strip()
        if location:
            parsed = urllib.parse.urlparse(location)
            suffix = Path(parsed.path or "").suffix.lower()
            if suffix in allowed:
                return suffix
    return ".mp4"


def _existing_recording_for_egress(room: str, egress_id: str) -> Path | None:
    safe_room = _safe_filename_component(room)
    safe_egress_id = _safe_filename_component(egress_id)
    matches = sorted(
        _recordings_dir().glob(f"{safe_room}__*__{safe_egress_id}.*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _local_source_paths_from_egress_file(file_result: Any) -> list[Path]:
    candidates: list[Path] = []
    filename = str(getattr(file_result, "filename", "") or "").strip()
    if filename:
        candidates.append(Path(filename))
    location = str(getattr(file_result, "location", "") or "").strip()
    if location:
        parsed = urllib.parse.urlparse(location)
        if parsed.scheme in ("", "file"):
            path_text = parsed.path if parsed.scheme == "file" else location
            if path_text:
                candidates.append(Path(path_text))
    return candidates


def _download_egress_recording_from_url(*, location: str, target: Path) -> bool:
    if not location:
        return False
    parsed = urllib.parse.urlparse(location)
    if parsed.scheme not in ("http", "https"):
        return False
    try:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(location, timeout=60, context=ssl_ctx) as resp:
            payload = resp.read()
        if not payload:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return True
    except Exception:  # noqa: BLE001
        logging.exception("recording_url_download_failed location=%s target=%s", location, target)
        return False


def _capture_egress_recording(room: str, info: EgressInfo) -> Path | None:
    egress_id = str(getattr(info, "egress_id", "") or "").strip()
    if not egress_id:
        return None
    existing = _existing_recording_for_egress(room, egress_id)
    if existing is not None and existing.exists():
        return existing

    file_results = list(getattr(info, "file_results", []) or [])
    if not file_results:
        return None

    ext = _recording_extension_from_egress(info)
    destination = _recordings_dir() / _recording_filename_for_egress(room, info, ext)
    destination.parent.mkdir(parents=True, exist_ok=True)

    for result in file_results:
        for source_path in _local_source_paths_from_egress_file(result):
            try:
                if source_path.exists() and source_path.is_file():
                    if source_path.resolve() == destination.resolve():
                        return destination
                    destination.write_bytes(source_path.read_bytes())
                    return destination
            except Exception:  # noqa: BLE001
                logging.exception(
                    "recording_local_copy_failed source=%s destination=%s egress_id=%s",
                    source_path,
                    destination,
                    egress_id,
                )
        location = str(getattr(result, "location", "") or "").strip()
        if _download_egress_recording_from_url(location=location, target=destination):
            return destination
    return None


async def _list_room_egress(room: str, *, active: bool | None = None) -> list[EgressInfo]:
    req = ListEgressRequest(room_name=room)
    if active is not None:
        req.active = active
    async with api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    ) as lk:
        response = await lk.egress.list_egress(req)
    return list(response.items or [])


def _friendly_egress_error(exc: Exception) -> str:
    raw = str(exc or "").strip()
    lowered = raw.lower()
    if "egress not connected" in lowered or "redis required" in lowered:
        return "LiveKit egress is not configured on the server. Enable Redis and the egress service in LiveKit to use recording."
    if "requested room does not exist" in lowered:
        return "Room is not active yet. Join the meeting first, then start recording."
    if raw:
        return raw
    return "unknown egress error"


def _latest_live_egress(infos: list[EgressInfo]) -> EgressInfo | None:
    live = [item for item in infos if _is_egress_live(int(getattr(item, "status", 0) or 0))]
    if not live:
        return None
    return sorted(live, key=_egress_sort_key, reverse=True)[0]


def _latest_terminal_egress(infos: list[EgressInfo]) -> EgressInfo | None:
    done = [item for item in infos if _is_egress_terminal(int(getattr(item, "status", 0) or 0))]
    if not done:
        return None
    return sorted(done, key=_egress_sort_key, reverse=True)[0]


def _recording_status_response(*, room: str, live: EgressInfo | None) -> RecordingStatusResponse:
    runtime = _recording_runtime_for_room(room)
    return RecordingStatusResponse(
        room=room,
        is_recording=live is not None,
        egress_id=str(getattr(live, "egress_id", "") or "") if live else "",
        egress_status=_egress_status_name(int(getattr(live, "status", 0) or 0)) if live else "",
        recording_available=bool(runtime.get("recording_available")),
        recording_filename=str(runtime.get("recording_filename") or ""),
        recording_size_bytes=int(runtime.get("recording_size_bytes") or 0),
        recording_updated_at=runtime.get("recording_updated_at"),
    )


async def _finalize_egress_recording(room: str, egress_id: str) -> None:
    task_key = f"{room}:{egress_id}"
    try:
        for _ in range(45):
            infos = await _list_room_egress(room)
            target = next((item for item in infos if str(getattr(item, "egress_id", "")) == egress_id), None)
            if target is None:
                await asyncio.sleep(1.0)
                continue
            status = int(getattr(target, "status", 0) or 0)
            if _is_egress_terminal(status):
                _capture_egress_recording(room, target)
                return
            await asyncio.sleep(1.0)
    except Exception:  # noqa: BLE001
        logging.exception("recording_finalize_failed room=%s egress_id=%s", room, egress_id)
    finally:
        recording_finalize_tasks.pop(task_key, None)


def _latest_recording_path(room: str) -> Path | None:
    normalized_room = str(room or "").strip()
    if not normalized_room:
        return None
    directory = _recordings_dir()
    if not directory.exists():
        return None
    safe_room = _safe_filename_component(normalized_room)
    matches = sorted(directory.glob(f"{safe_room}__*"), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _recording_runtime_for_room(room: str) -> dict[str, Any]:
    path = _latest_recording_path(room)
    if path is None or not path.exists():
        return {
            "recording_available": False,
            "recording_size_bytes": 0,
            "recording_filename": "",
            "recording_updated_at": None,
        }
    stat = path.stat()
    return {
        "recording_available": True,
        "recording_size_bytes": int(stat.st_size),
        "recording_filename": path.name,
        "recording_updated_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
    }


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
    enriched.update(_recording_runtime_for_room(room))
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

    @app.get("/api/settings/agent-prompts", response_model=AgentPromptListResponse)
    async def list_agent_prompts(username: str = Depends(require_session_user)) -> AgentPromptListResponse:
        del username
        records = [AgentPromptRecord(**item) for item in list_prompt_records()]
        return AgentPromptListResponse(prompts=records)

    @app.put("/api/settings/agent-prompts/{agent}", response_model=AgentPromptRecord)
    async def upsert_agent_prompt(
        agent: str,
        payload: AgentPromptUpdateRequest,
        username: str = Depends(require_session_user),
    ) -> AgentPromptRecord:
        del username
        try:
            updated = set_prompt(agent, payload.prompt)
            return AgentPromptRecord(**updated)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/settings/agent-prompts/{agent}/reset", response_model=AgentPromptRecord)
    async def reset_agent_prompt(agent: str, username: str = Depends(require_session_user)) -> AgentPromptRecord:
        del username
        try:
            reset = reset_prompt(agent)
            return AgentPromptRecord(**reset)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/token", response_model=TokenResponse)
    async def create_token(payload: TokenRequest, username: str = Depends(require_session_user)) -> TokenResponse:
        metrics.token_requests += 1

        try:
            selected_agent = payload.agent.strip().lower()
            resolved_instructions = (payload.instructions or "").strip()
            if payload.ai_enabled and not resolved_instructions:
                resolved_instructions = get_effective_prompt(selected_agent)
            requested_room = payload.room.strip()
            if not requested_room:
                raise HTTPException(status_code=400, detail="room is required")

            room_name = requested_room
            observer_suffix = "-observer"
            if selected_agent == "observer" and not room_name.endswith(observer_suffix):
                room_name = f"{room_name}{observer_suffix}"
            resolved_instructions = _expand_interview_context_placeholders(resolved_instructions, room_name)

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
                        instructions=resolved_instructions or None,
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
                                instructions=resolved_instructions or None,
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
                                instructions=resolved_instructions or None,
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
                            instructions=resolved_instructions or None,
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
            if payload.ai_enabled and resolved_instructions:
                try:
                    _inject_prompt_trace_at_transcript_start(
                        room=room_name,
                        prompt=resolved_instructions,
                        username=username,
                    )
                except Exception:  # noqa: BLE001
                    logging.exception("prompt trace injection failed room=%s user=%s", room_name, username)

            return TokenResponse(
                token=token,
                server_url=settings.livekit_url,
                identity=identity,
                room=room_name,
                expires_at=expires_at.isoformat(),
                instructions=resolved_instructions,
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

    @app.post("/api/recordings/start", response_model=RecordingStatusResponse)
    async def start_recording(
        payload: RecordingControlRequest,
        username: str = Depends(require_session_user),
    ) -> RecordingStatusResponse:
        del username
        normalized_room = payload.room.strip()
        if not normalized_room:
            raise HTTPException(status_code=400, detail="room is required")

        try:
            current = await _list_room_egress(normalized_room, active=True)
            live = _latest_live_egress(current)
            if live is not None:
                return _recording_status_response(room=normalized_room, live=live)

            stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
            safe_room = _safe_filename_component(normalized_room)
            target_rel_path = f"/recordings/{safe_room}__{stamp}.mp4"
            request = RoomCompositeEgressRequest(
                room_name=normalized_room,
                layout="speaker-dark",
                file_outputs=[EncodedFileOutput(file_type=EncodedFileType.MP4, filepath=target_rel_path)],
            )
            async with api.LiveKitAPI(
                url=settings.livekit_url,
                api_key=settings.livekit_api_key,
                api_secret=settings.livekit_api_secret,
            ) as lk:
                info = await lk.egress.start_room_composite_egress(request)
            return _recording_status_response(room=normalized_room, live=info)
        except Exception as exc:  # noqa: BLE001
            logging.exception("recording_start_failed room=%s", normalized_room)
            raise HTTPException(status_code=503, detail=f"unable to start recording: {_friendly_egress_error(exc)}") from exc

    @app.post("/api/recordings/stop", response_model=RecordingStatusResponse)
    async def stop_recording(
        payload: RecordingControlRequest,
        username: str = Depends(require_session_user),
    ) -> RecordingStatusResponse:
        del username
        normalized_room = payload.room.strip()
        if not normalized_room:
            raise HTTPException(status_code=400, detail="room is required")

        try:
            current = await _list_room_egress(normalized_room, active=True)
            live = _latest_live_egress(current)
            if live is None:
                all_infos = await _list_room_egress(normalized_room)
                latest_done = _latest_terminal_egress(all_infos)
                if latest_done is not None:
                    _capture_egress_recording(normalized_room, latest_done)
                return _recording_status_response(room=normalized_room, live=None)

            egress_id = str(getattr(live, "egress_id", "") or "")
            async with api.LiveKitAPI(
                url=settings.livekit_url,
                api_key=settings.livekit_api_key,
                api_secret=settings.livekit_api_secret,
            ) as lk:
                info = await lk.egress.stop_egress(StopEgressRequest(egress_id=egress_id))

            if egress_id:
                task_key = f"{normalized_room}:{egress_id}"
                existing_task = recording_finalize_tasks.get(task_key)
                if existing_task is None or existing_task.done():
                    recording_finalize_tasks[task_key] = asyncio.create_task(
                        _finalize_egress_recording(normalized_room, egress_id)
                    )

            next_live = info if _is_egress_live(int(getattr(info, "status", 0) or 0)) else None
            return _recording_status_response(room=normalized_room, live=next_live)
        except Exception as exc:  # noqa: BLE001
            logging.exception("recording_stop_failed room=%s", normalized_room)
            raise HTTPException(status_code=503, detail=f"unable to stop recording: {_friendly_egress_error(exc)}") from exc

    @app.get("/api/recordings/{room}/status", response_model=RecordingStatusResponse)
    async def recording_status(
        room: str,
        username: str = Depends(require_session_user),
    ) -> RecordingStatusResponse:
        del username
        normalized_room = room.strip()
        if not normalized_room:
            raise HTTPException(status_code=400, detail="room is required")

        try:
            infos = await _list_room_egress(normalized_room)
            live = _latest_live_egress(infos)
            latest_done = _latest_terminal_egress(infos)
            if latest_done is not None:
                _capture_egress_recording(normalized_room, latest_done)
            return _recording_status_response(room=normalized_room, live=live)
        except Exception as exc:  # noqa: BLE001
            detail = _friendly_egress_error(exc)
            if "not configured" in detail:
                return _recording_status_response(room=normalized_room, live=None)
            logging.exception("recording_status_failed room=%s", normalized_room)
            raise HTTPException(status_code=503, detail=f"unable to fetch recording status: {detail}") from exc

    @app.get("/api/recordings/{room}/download")
    async def download_recording(
        room: str,
        username: str = Depends(require_session_user),
    ) -> Response:
        del username
        normalized_room = room.strip()
        path = _latest_recording_path(normalized_room)
        if path is None or not path.exists():
            raise HTTPException(status_code=404, detail="recording not found")

        content_type = "application/octet-stream"
        suffix = path.suffix.lower()
        if suffix == ".webm":
            content_type = "video/webm"
        elif suffix == ".mp4":
            content_type = "video/mp4"
        elif suffix == ".m4a":
            content_type = "audio/mp4"
        elif suffix == ".wav":
            content_type = "audio/wav"

        return Response(
            content=path.read_bytes(),
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
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
            duration_minutes=payload.duration_minutes,
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
