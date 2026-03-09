from __future__ import annotations

import json
import logging
import re
import ssl
import threading
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import certifi

from app.positions_service import CANONICAL_SKILL_MAP, extract_text_from_file
from shared.config import settings

SKILL_NOISE_WORDS = {
    "experience",
    "experienced",
    "expertise",
    "proficiency",
    "knowledge",
    "understanding",
    "years",
    "year",
}

CANDIDATE_FIELDS_REQUIRED = ["fullName", "email", "keySkills"]

_candidates_lock = threading.Lock()


def _candidates_file_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "candidates.json"


def _cv_storage_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "cvs"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned[:128] or "file"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).strip()
    if not text:
        return None
    matched = re.search(r"\d+(?:\.\d+)?", text)
    if not matched:
        return None
    try:
        return round(float(matched.group(0)), 2)
    except ValueError:
        return None


def _split_items(values: list[str] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        chunks = re.split(r"[,\n]", values)
    else:
        chunks = values

    output: list[str] = []
    seen: set[str] = set()
    for raw in chunks:
        text = str(raw).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _canonicalize_skill(skill: str) -> str:
    text = re.sub(r"\s+", " ", str(skill).strip())
    text = re.sub(r"\([^)]*\)", "", text).strip()
    text = re.sub(r"\s*[<>=~!]{1,2}\s*\d+(?:\.\d+){0,2}", "", text).strip()
    text = re.sub(
        r"\b(experience|proficiency|expertise|knowledge|understanding|skills?)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip(" -,:;")
    if not text:
        return ""

    lowered_words = set(text.casefold().split())
    if lowered_words & SKILL_NOISE_WORDS == lowered_words:
        return ""

    compact = re.sub(r"[^a-z0-9#+./\s]", "", text.casefold())
    compact = re.sub(r"\s+", " ", compact).strip()
    canonical = CANONICAL_SKILL_MAP.get(compact)
    if canonical:
        return canonical

    matched_tokens: list[str] = []
    seen: set[str] = set()
    lowered = text.casefold()
    for alias, normalized in CANONICAL_SKILL_MAP.items():
        if len(alias) < 2:
            continue
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            matched_tokens.append(normalized)

    if matched_tokens:
        return matched_tokens[0]

    return text


def _clean_key_skills(values: list[str] | str | None) -> list[str]:
    raw_items = _split_items(values)
    output: list[str] = []
    seen: set[str] = set()

    for raw in raw_items:
        parts = re.split(r"\s*(?:/|\band\b|\bor\b|;)\s*", raw, flags=re.IGNORECASE)
        for part in parts:
            item = _canonicalize_skill(part)
            if not item:
                continue
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            output.append(item)

    return output


def _clean_cv_metadata(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    metadata = {
        "originalName": str(raw.get("originalName") or "").strip(),
        "storedName": str(raw.get("storedName") or "").strip(),
        "contentType": str(raw.get("contentType") or "").strip(),
        "size": int(raw.get("size")) if isinstance(raw.get("size"), int) else 0,
    }
    if not any(metadata.values()):
        return None
    return metadata


def _clean_screening_cache(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        skey = str(key).strip()
        if not skey:
            continue
        cleaned[skey] = value
    return cleaned or None


def _normalize_candidate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "fullName": str(payload.get("fullName") or "").strip(),
        "email": str(payload.get("email") or "").strip(),
        "currentTitle": str(payload.get("currentTitle") or "").strip(),
        "yearsExperience": _to_float(payload.get("yearsExperience")),
        "keySkills": _clean_key_skills(payload.get("keySkills")),
        "keyProjectHighlights": _split_items(payload.get("keyProjectHighlights")),
        "candidateContext": str(payload.get("candidateContext") or "").strip(),
        "cvTextSummary": str(payload.get("cvTextSummary") or "").strip(),
        "cvMetadata": _clean_cv_metadata(payload.get("cvMetadata")),
        "screeningCache": _clean_screening_cache(payload.get("screeningCache")),
    }
    return normalized


def _write_candidates(candidates: list[dict[str, Any]]) -> None:
    path = _candidates_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(candidates, fp, ensure_ascii=False, indent=2)


def load_candidates() -> list[dict[str, Any]]:
    path = _candidates_file_path()
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except json.JSONDecodeError:
        logging.exception("candidates file is corrupted")
        return []

    if not isinstance(data, list):
        return []

    cleaned: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        base = _normalize_candidate_payload(row)
        base["id"] = str(row.get("id") or "")
        base["createdAt"] = str(row.get("createdAt") or "")
        base["updatedAt"] = str(row.get("updatedAt") or "")
        cleaned.append(base)

    cleaned.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
    return cleaned


def create_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    now = _now_iso()
    with _candidates_lock:
        candidates = load_candidates()
        normalized = _normalize_candidate_payload(payload)
        normalized["id"] = str(uuid.uuid4())
        normalized["createdAt"] = now
        normalized["updatedAt"] = now
        candidates.append(normalized)
        candidates.sort(key=lambda item: item["updatedAt"], reverse=True)
        _write_candidates(candidates)
    return normalized


def get_candidate(candidate_id: str) -> dict[str, Any] | None:
    for row in load_candidates():
        if row.get("id") == candidate_id:
            return row
    return None


def update_candidate(candidate_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    now = _now_iso()
    with _candidates_lock:
        candidates = load_candidates()
        for idx, row in enumerate(candidates):
            if row.get("id") != candidate_id:
                continue
            merged = _normalize_candidate_payload(payload)
            merged["id"] = row["id"]
            merged["createdAt"] = row.get("createdAt", now)
            merged["updatedAt"] = now
            candidates[idx] = merged
            candidates.sort(key=lambda item: item["updatedAt"], reverse=True)
            _write_candidates(candidates)
            return merged
    return None


def delete_candidate(candidate_id: str) -> bool:
    with _candidates_lock:
        candidates = load_candidates()
        remaining = [row for row in candidates if row.get("id") != candidate_id]
        if len(remaining) == len(candidates):
            return False
        _write_candidates(remaining)
        return True


def persist_cv_file(filename: str, content: bytes) -> dict[str, Any]:
    ext = Path(filename).suffix.lower() or ".bin"
    safe = _safe_filename_component(Path(filename).stem)
    stored_name = f"{uuid.uuid4().hex}_{safe}{ext}"
    path = _cv_storage_dir() / stored_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    content_type = "application/octet-stream"
    if ext == ".pdf":
        content_type = "application/pdf"
    elif ext == ".txt":
        content_type = "text/plain"
    elif ext in {".docx", ".doc"}:
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    return {
        "originalName": filename,
        "storedName": stored_name,
        "contentType": content_type,
        "size": len(content),
    }


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _llm_extract_candidate(cv_text: str) -> dict[str, Any]:
    schema = {
        "name": "candidate_extraction",
        "schema": {
            "type": "object",
            "properties": {
                "fullName": {"type": "string"},
                "email": {"type": "string"},
                "currentTitle": {"type": "string"},
                "yearsExperience": {"type": ["number", "null"]},
                "keySkills": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 32,
                        "pattern": r"^[A-Za-z0-9+.#/&-]+(?: [A-Za-z0-9+.#/&-]+){0,2}$",
                    },
                },
                "keyProjectHighlights": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string", "maxLength": 180},
                },
                "candidateContext": {"type": "string"},
            },
            "required": [
                "fullName",
                "email",
                "currentTitle",
                "yearsExperience",
                "keySkills",
                "keyProjectHighlights",
                "candidateContext",
            ],
            "additionalProperties": False,
        },
    }

    payload = {
        "model": settings.llm_model,
        "temperature": 0.1,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract candidate profile details from a resume/CV. "
                    "Use only evidence from the CV. "
                    "Return atomic keySkills as canonical keyword tags (1-3 words), not narrative phrases. "
                    "Include only technical/professional skills."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Extract fullName, email, currentTitle, yearsExperience, keySkills, keyProjectHighlights, candidateContext from this CV:\n\n"
                    + cv_text[:30000]
                ),
            },
        ],
        "response_format": {"type": "json_schema", "json_schema": schema},
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=35, context=ssl_ctx) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise ValueError(f"OpenAI candidate extraction failed: {exc.code} {detail[:300]}") from exc

    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    parsed = _extract_json_object(content)
    if parsed is None:
        raise ValueError("unable to parse candidate extraction response")
    return parsed


def _heuristic_extract_candidate(cv_text: str) -> dict[str, Any]:
    lines = [line.strip() for line in cv_text.splitlines() if line.strip()]
    full_name = lines[0] if lines else ""
    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", cv_text)
    email = email_match.group(0) if email_match else ""

    current_title = ""
    for line in lines[:12]:
        if re.search(r"engineer|developer|consultant|architect|manager|analyst|lead|sdet|qa", line, flags=re.IGNORECASE):
            current_title = line
            break

    years = None
    years_match = re.search(r"(\d+(?:\.\d+)?)\+?\s+years", cv_text, flags=re.IGNORECASE)
    if years_match:
        years = float(years_match.group(1))

    found_skills: list[str] = []
    seen: set[str] = set()
    lowered = cv_text.casefold()
    for alias, canonical in CANONICAL_SKILL_MAP.items():
        if len(alias) < 2:
            continue
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            key = canonical.casefold()
            if key in seen:
                continue
            seen.add(key)
            found_skills.append(canonical)
        if len(found_skills) >= 20:
            break

    highlights = [line for line in lines if len(line) > 30][:5]
    context = " ".join(lines[:3])[:300]

    return {
        "fullName": full_name,
        "email": email,
        "currentTitle": current_title,
        "yearsExperience": years,
        "keySkills": found_skills,
        "keyProjectHighlights": highlights,
        "candidateContext": context,
    }


def build_cv_text_summary(cv_text: str, *, max_chars: int = 1200) -> str:
    cleaned = re.sub(r"\s+", " ", cv_text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    head = cleaned[:max_chars].rsplit(" ", 1)[0].strip()
    return f"{head}..."


def extract_candidate_details(cv_text: str) -> tuple[dict[str, Any], bool, list[str]]:
    warnings: list[str] = []
    used_llm = False

    if settings.openai_api_key:
        try:
            extracted = _llm_extract_candidate(cv_text)
            used_llm = True
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            logging.exception("candidate llm extraction failed; using heuristic")
            warnings.append(f"LLM extraction unavailable: {exc}")
            extracted = _heuristic_extract_candidate(cv_text)
    else:
        warnings.append("OPENAI_API_KEY not configured; used heuristic extraction")
        extracted = _heuristic_extract_candidate(cv_text)

    normalized = _normalize_candidate_payload(extracted)
    if not normalized.get("cvTextSummary"):
        normalized["cvTextSummary"] = build_cv_text_summary(cv_text)
    return normalized, used_llm, warnings


def extract_candidate_from_file(filename: str, raw_bytes: bytes) -> tuple[str, dict[str, Any]]:
    text = extract_text_from_file(filename, raw_bytes)
    metadata = persist_cv_file(filename, raw_bytes)
    return text, metadata
