from __future__ import annotations

import io
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

from shared.config import settings

POSITION_REQUIRED_FIELDS = ["role_title", "must_haves", "tech_stack", "focus_areas"]
LIST_FIELDS = ["must_haves", "nice_to_haves", "tech_stack", "focus_areas"]
SKILL_LIST_FIELDS = {"must_haves", "nice_to_haves", "tech_stack"}
CONFIDENCE_FIELDS = [
    "role_title",
    "level",
    "must_haves",
    "nice_to_haves",
    "tech_stack",
    "focus_areas",
    "evaluation_policy",
]

_positions_lock = threading.Lock()

CANONICAL_SKILL_MAP = {
    "py": "Python",
    "python": "Python",
    "js": "JavaScript",
    "javascript": "JavaScript",
    "ecmascript": "JavaScript",
    "ts": "TypeScript",
    "typescript": "TypeScript",
    "node": "Node.js",
    "nodejs": "Node.js",
    "node.js": "Node.js",
    "reactjs": "React",
    "react.js": "React",
    "react": "React",
    "nextjs": "Next.js",
    "next.js": "Next.js",
    "vuejs": "Vue.js",
    "vue": "Vue.js",
    "angularjs": "Angular",
    "angular": "Angular",
    "golang": "Go",
    "go": "Go",
    "java": "Java",
    "c#": "C#",
    "dotnet": ".NET",
    ".net": ".NET",
    "sql": "SQL",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "nosql": "NoSQL",
    "mongodb": "MongoDB",
    "redis": "Redis",
    "aws": "AWS",
    "amazon web services": "AWS",
    "gcp": "GCP",
    "google cloud": "GCP",
    "azure": "Azure",
    "docker": "Docker",
    "k8s": "Kubernetes",
    "kubernetes": "Kubernetes",
    "terraform": "Terraform",
    "ansible": "Ansible",
    "ci cd": "CI/CD",
    "ci/cd": "CI/CD",
    "github actions": "GitHub Actions",
    "jenkins": "Jenkins",
    "airflow": "Airflow",
    "spark": "Spark",
    "hadoop": "Hadoop",
    "fastapi": "FastAPI",
    "flask": "Flask",
    "django": "Django",
    "llm": "LLM",
    "llms": "LLM",
    "rag": "RAG",
    "rag workflow": "RAG",
    "rag workflows": "RAG",
    "rag system": "RAG",
    "rag systems": "RAG",
    "vector database": "Vector Database",
    "vector databases": "Vector Database",
    "mcp": "MCP",
    "mcp server": "MCP",
    "mcp servers": "MCP",
    "genai": "Generative AI",
    "generative ai": "Generative AI",
    "nlp": "NLP",
    "ml": "Machine Learning",
    "machine learning": "Machine Learning",
    "deep learning": "Deep Learning",
}

CANONICAL_FOCUS_MAP = {
    "backend": "Backend",
    "frontend": "Frontend",
    "full stack": "Full Stack",
    "fullstack": "Full Stack",
    "data engineering": "Data Engineering",
    "data platform": "Data Platform",
    "machine learning": "Machine Learning",
    "platform": "Platform",
    "devops": "DevOps",
    "security": "Security",
    "testing": "Testing",
    "qa": "Testing",
    "architecture": "Architecture",
}

SKILL_NOISE_PHRASES = {
    "experience",
    "years experience",
    "year experience",
    "proficiency",
    "knowledge",
    "understanding",
    "skills",
    "skill",
    "expertise",
}

SKILL_NOISE_TOKENS = {
    "migration",
    "migrations",
    "workshop",
    "workshops",
    "stakeholder",
    "stakeholders",
    "process",
    "processes",
}


def _positions_file_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "positions.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_skill_text(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"^[\-*•\d\.\)\s]+", "", text)
    text = re.sub(
        r"^(required|requirements?|must[-\s]?have|must|mandatory|preferred|nice[-\s]?to[-\s]?have|plus|good[-\s]?to[-\s]?have)\s*[:\-]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _sanitize_skill_phrase(raw: str) -> str:
    text = _normalize_skill_text(raw)
    if not text:
        return ""

    # Drop explicit years/tenure requirements from skill tags.
    if re.search(r"\b\d+\s*[-+]?\s*\d*\s*years?\b", text, flags=re.IGNORECASE):
        return ""
    if re.search(r"^\d+\+?\s*years?\b", text, flags=re.IGNORECASE):
        return ""

    # Remove version/comparator details from otherwise valid skills.
    text = re.sub(r"\([^)]*[<>=~!][^)]*\)", "", text)
    text = re.sub(r"\s*[<>=~!]{1,2}\s*\d+(?:\.\d+){0,2}", "", text)
    text = re.sub(r"\b(Python|Java|Node\.js|PostgreSQL|MySQL|TypeScript|JavaScript)\s+\d+(?:\.\d+){0,2}\b", r"\1", text, flags=re.IGNORECASE)

    # Convert qualification-style phrases into pure skill tags.
    text = re.sub(
        r"\b(experience|proficiency|expertise|knowledge|understanding|skills?)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^\s*(with|of|in|on|for|and)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -,:;")

    if not text:
        return ""
    if re.search(r"\byears?\b", text, flags=re.IGNORECASE):
        return ""
    if any(token in text.casefold().split() for token in SKILL_NOISE_TOKENS):
        return ""

    lowered = text.casefold()
    if lowered in SKILL_NOISE_PHRASES:
        return ""

    return text


def _canonicalize_skill(raw: str) -> str:
    text = _sanitize_skill_phrase(raw)
    if not text:
        return ""
    lowered = text.casefold()
    compact = re.sub(r"[^a-z0-9#+./\s]", "", lowered)
    compact = re.sub(r"\s+", " ", compact).strip()
    canonical = CANONICAL_SKILL_MAP.get(compact)
    if canonical:
        return canonical
    return text


def _extract_known_skills_from_text(text: str) -> list[str]:
    lowered = text.casefold()
    found: list[str] = []
    seen: set[str] = set()
    for alias, canonical in CANONICAL_SKILL_MAP.items():
        if len(alias) < 2:
            continue
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            key = canonical.casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append(canonical)
    return found


def _expand_skill_item(raw: str) -> list[str]:
    base = _sanitize_skill_phrase(raw)
    if not base:
        return []

    parts = re.split(r"\s*(?:,|/|;|\||\band\b|\bor\b)\s*", base, flags=re.IGNORECASE)
    tokens: list[str] = []
    seen: set[str] = set()

    def _append(token: str) -> None:
        key = token.casefold()
        if key in seen:
            return
        seen.add(key)
        tokens.append(token)

    for part in parts:
        part = _sanitize_skill_phrase(part)
        if not part:
            continue
        inferred_list = _extract_known_skills_from_text(part)
        for inferred in inferred_list:
            _append(inferred)
        canonical = _canonicalize_skill(part)
        # Keep original canonical phrase only when short/atomic, or when we found nothing else.
        if canonical and (len(canonical.split()) <= 3 or not inferred_list):
            _append(canonical)

    if not tokens:
        canonical = _canonicalize_skill(base)
        if canonical:
            _append(canonical)
        for inferred in _extract_known_skills_from_text(base):
            _append(inferred)

    return tokens


def _canonicalize_focus(raw: str) -> str:
    text = _normalize_skill_text(raw)
    lowered = text.casefold()
    canonical = CANONICAL_FOCUS_MAP.get(lowered)
    if canonical:
        return canonical
    return text.title()


def _clean_list(
    values: list[str] | str | None,
    *,
    field_name: str | None = None,
    apply_skill_guardrails: bool = True,
) -> list[str]:
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
        if field_name in SKILL_LIST_FIELDS and apply_skill_guardrails:
            expanded = _expand_skill_item(text)
            for token in expanded:
                key = token.casefold()
                if key in seen:
                    continue
                seen.add(key)
                output.append(token)
            continue
        if field_name in SKILL_LIST_FIELDS and not apply_skill_guardrails:
            text = _normalize_skill_text(text)
        elif field_name == "focus_areas":
            text = _canonicalize_focus(text)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _clean_confidence(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        raw = {}

    confidence: dict[str, float] = {}
    for key in CONFIDENCE_FIELDS:
        value = raw.get(key)
        if not isinstance(value, (int, float)):
            continue
        confidence[key] = max(0.0, min(1.0, float(value)))

    if "overall" in raw and isinstance(raw["overall"], (int, float)):
        confidence["overall"] = max(0.0, min(1.0, float(raw["overall"])))

    if "overall" not in confidence:
        field_scores = [confidence.get(key, 0.0) for key in CONFIDENCE_FIELDS]
        confidence["overall"] = round(sum(field_scores) / len(field_scores), 3)

    return confidence


def _infer_missing_fields(position: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field in POSITION_REQUIRED_FIELDS:
        value = position.get(field)
        if isinstance(value, list):
            if not value:
                missing.append(field)
            continue
        if not str(value or "").strip():
            missing.append(field)
    return missing


def _normalize_position_payload(payload: dict[str, Any], *, apply_skill_guardrails: bool = True) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "role_title": str(payload.get("role_title") or "").strip(),
        "jd_text": str(payload.get("jd_text") or "").strip(),
        "level": str(payload.get("level") or "").strip(),
        "evaluation_policy": str(payload.get("evaluation_policy") or "").strip(),
    }

    for field in LIST_FIELDS:
        normalized[field] = _clean_list(
            payload.get(field),
            field_name=field,
            apply_skill_guardrails=apply_skill_guardrails,
        )

    # Keep must_haves and nice_to_haves distinct after canonicalization.
    must_keys = {item.casefold() for item in normalized["must_haves"]}
    normalized["nice_to_haves"] = [
        item for item in normalized["nice_to_haves"] if item.casefold() not in must_keys
    ]

    normalized["extraction_confidence"] = _clean_confidence(payload.get("extraction_confidence"))

    missing_fields = payload.get("missing_fields")
    if isinstance(missing_fields, list):
        normalized["missing_fields"] = _clean_list(missing_fields, field_name="missing_fields")
    else:
        normalized["missing_fields"] = _infer_missing_fields(normalized)

    return normalized


def load_positions() -> list[dict[str, Any]]:
    path = _positions_file_path()
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except json.JSONDecodeError:
        logging.exception("positions file is corrupted")
        return []

    if not isinstance(data, list):
        return []

    cleaned: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        # Preserve stored manual edits as-is when reading.
        base = _normalize_position_payload(row, apply_skill_guardrails=False)
        base["position_id"] = str(row.get("position_id") or "")
        base["created_by"] = str(row.get("created_by") or "")
        base["created_at"] = str(row.get("created_at") or "")
        base["updated_at"] = str(row.get("updated_at") or "")
        version = row.get("version")
        base["version"] = int(version) if isinstance(version, int) and version > 0 else 1
        cleaned.append(base)

    cleaned.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return cleaned


def _write_positions(positions: list[dict[str, Any]]) -> None:
    path = _positions_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(positions, fp, ensure_ascii=False, indent=2)


def create_position(payload: dict[str, Any], created_by: str) -> dict[str, Any]:
    now = _now_iso()
    with _positions_lock:
        positions = load_positions()
        normalized = _normalize_position_payload(payload, apply_skill_guardrails=True)
        normalized["position_id"] = str(uuid.uuid4())
        normalized["created_by"] = created_by
        normalized["created_at"] = now
        normalized["updated_at"] = now
        normalized["version"] = 1
        positions.append(normalized)
        positions.sort(key=lambda item: item["updated_at"], reverse=True)
        _write_positions(positions)
    return normalized


def get_position(position_id: str) -> dict[str, Any] | None:
    for row in load_positions():
        if row.get("position_id") == position_id:
            return row
    return None


def update_position(position_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    now = _now_iso()
    with _positions_lock:
        positions = load_positions()
        for idx, row in enumerate(positions):
            if row.get("position_id") != position_id:
                continue

            # Manual edits should not be auto-normalized into extracted guardrails.
            merged = _normalize_position_payload(payload, apply_skill_guardrails=False)
            merged["position_id"] = row["position_id"]
            merged["created_by"] = row.get("created_by", "")
            merged["created_at"] = row.get("created_at", now)
            merged["updated_at"] = now
            merged["version"] = int(row.get("version", 1)) + 1
            positions[idx] = merged
            positions.sort(key=lambda item: item["updated_at"], reverse=True)
            _write_positions(positions)
            return merged

    return None


def delete_position(position_id: str) -> bool:
    with _positions_lock:
        positions = load_positions()
        remaining = [row for row in positions if row.get("position_id") != position_id]
        if len(remaining) == len(positions):
            return False
        _write_positions(remaining)
        return True


def extract_text_from_file(filename: str, raw_bytes: bytes) -> str:
    suffix = Path(filename).suffix.lower()

    if suffix in {".txt", ".md"}:
        for encoding in ("utf-8", "utf-16", "latin-1"):
            try:
                return raw_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw_bytes.decode("utf-8", errors="ignore")

    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw_bytes))
        text = "\n".join((page.extract_text() or "").strip() for page in reader.pages)
        return text.strip()

    if suffix == ".docx":
        from docx import Document

        document = Document(io.BytesIO(raw_bytes))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        return text.strip()

    if suffix == ".doc":
        raise ValueError("legacy .doc is not supported; please upload .docx")

    raise ValueError("unsupported file type")


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


def _llm_extract(jd_text: str) -> dict[str, Any]:
    keyword_item_schema = {
        "type": "string",
        "minLength": 1,
        "maxLength": 32,
        "pattern": r"^[A-Za-z0-9+.#/&-]+(?: [A-Za-z0-9+.#/&-]+){0,2}$",
    }
    schema = {
        "name": "position_extraction",
        "schema": {
            "type": "object",
            "properties": {
                "role_title": {"type": "string"},
                "level": {"type": "string"},
                "must_haves": {"type": "array", "maxItems": 12, "items": keyword_item_schema},
                "nice_to_haves": {"type": "array", "maxItems": 12, "items": keyword_item_schema},
                "tech_stack": {"type": "array", "maxItems": 12, "items": keyword_item_schema},
                "focus_areas": {"type": "array", "maxItems": 12, "items": keyword_item_schema},
                "evaluation_policy": {"type": "string"},
                "extraction_confidence": {
                    "type": "object",
                    "properties": {**{k: {"type": "number"} for k in CONFIDENCE_FIELDS}, "overall": {"type": "number"}},
                    "required": ["overall"],
                    "additionalProperties": False,
                },
                "missing_fields": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "role_title",
                "level",
                "must_haves",
                "nice_to_haves",
                "tech_stack",
                "focus_areas",
                "evaluation_policy",
                "extraction_confidence",
                "missing_fields",
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
                    "You extract structured hiring position fields from a job description. "
                    "Use only evidence from the JD and avoid hallucinations. "
                    "Rules: "
                    "1) role_title should be a concrete role (e.g. Senior Data Engineer), inferred if needed. "
                    "2) must_haves include explicitly required/mandatory/minimum qualifications. "
                    "3) nice_to_haves include preferred/plus/good-to-have items only. "
                    "4) tech_stack should include technologies/tools/platforms mentioned in the JD; no soft skills. "
                    "5) list fields must contain atomic skill tags, not narrative text. "
                    "6) each list item should be a trivial skill token (technology/tool/concept), 1-3 words, max 32 characters. "
                    "7) keep arrays deduplicated and concise (max 12 items each). "
                    "8) use canonical skill names when obvious: Python, JavaScript, TypeScript, Node.js, React, SQL, PostgreSQL, AWS, GCP, Azure, Docker, Kubernetes, Terraform, CI/CD, Machine Learning, LLM. "
                    "9) if uncertain, return fewer items or empty list instead of verbose phrases."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Return JSON object with this exact shape:\n"
                    "{\n"
                    '  "role_title": string,\n'
                    '  "level": string,\n'
                    '  "must_haves": string[],\n'
                    '  "nice_to_haves": string[],\n'
                    '  "tech_stack": string[],\n'
                    '  "focus_areas": string[],\n'
                    '  "evaluation_policy": string,\n'
                    '  "extraction_confidence": {\n'
                    '    "role_title": number,\n'
                    '    "level": number,\n'
                    '    "must_haves": number,\n'
                    '    "nice_to_haves": number,\n'
                    '    "tech_stack": number,\n'
                    '    "focus_areas": number,\n'
                    '    "evaluation_policy": number,\n'
                    '    "overall": number\n'
                    "  },\n"
                    '  "missing_fields": string[]\n'
                    "}\n\n"
                    "Hard output constraints for list fields (must_haves, nice_to_haves, tech_stack, focus_areas):\n"
                    "- Output only trivial keyword tags, never clauses or sentences.\n"
                    "- 1 to 3 words per item.\n"
                    "- Avoid items containing commas, periods, colons, semicolons, or conjunctions like 'and/or'.\n"
                    "- Good: 'Python', 'RAG', 'LangChain', 'CI/CD'.\n"
                    "- Bad: 'Experience in Python and cloud technologies'.\n\n"
                    "Extract these fields from this JD:\n\n"
                    + jd_text[:30000]
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
        # Fallback to JSON mode for models/accounts that reject schema mode.
        fallback_payload = dict(payload)
        fallback_payload["response_format"] = {"type": "json_object"}
        fallback_req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(fallback_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(fallback_req, timeout=35, context=ssl_ctx) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as fallback_exc:
            fallback_detail = fallback_exc.read().decode("utf-8", errors="ignore")
            raise ValueError(
                f"OpenAI extraction request failed: {exc.code} {detail[:220]} | fallback failed: {fallback_exc.code} {fallback_detail[:220]}"
            ) from fallback_exc

    content = (
        body.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    parsed = _extract_json_object(content)
    if parsed is None:
        raise ValueError("unable to parse extraction response")
    return parsed


def _heuristic_extract(jd_text: str) -> dict[str, Any]:
    lines = [line.strip() for line in jd_text.splitlines() if line.strip()]
    role_title = ""
    for line in lines[:20]:
        matched = re.search(r"(job title|title|role|position)\s*[:\-]\s*(.+)", line, flags=re.IGNORECASE)
        if matched:
            role_title = matched.group(2).strip()
            break
    for line in lines[:8]:
        if role_title:
            break
        if len(line) <= 120 and not line.lower().startswith(("job description", "about", "company", "location", "team")):
            role_title = line
            break

    lowered = jd_text.lower()
    level = ""
    for candidate in ["intern", "junior", "mid", "senior", "lead", "staff", "principal", "manager"]:
        if re.search(rf"\\b{re.escape(candidate)}\\b", lowered):
            level = candidate.title()
            break

    skill_terms = [
        "python",
        "java",
        "javascript",
        "typescript",
        "react",
        "node",
        "sql",
        "postgres",
        "mysql",
        "aws",
        "azure",
        "gcp",
        "docker",
        "kubernetes",
        "fastapi",
        "django",
        "flask",
        "ci/cd",
        "terraform",
        "redis",
        "spark",
        "airflow",
        "ml",
        "llm",
    ]

    must_haves = [term for term in skill_terms if f"{term}" in lowered][:8]
    nice_to_haves = []
    preferred_lines = [line.casefold() for line in lines if re.search(r"\b(preferred|plus|good to have|nice to have)\b", line, flags=re.IGNORECASE)]
    for term in skill_terms:
        if term in must_haves:
            continue
        if any(term in row for row in preferred_lines):
            nice_to_haves.append(term)
    tech_stack = [term for term in must_haves if term in {"python", "java", "javascript", "typescript", "react", "node", "sql", "aws", "azure", "gcp", "docker", "kubernetes", "fastapi", "django", "flask", "postgres", "mysql", "redis", "spark", "airflow"}]
    focus_areas = [
        area
        for area in ["backend", "frontend", "data engineering", "machine learning", "platform", "devops", "testing", "security", "architecture"]
        if area in lowered
    ]

    evaluation_policy = ""
    for marker in ["interview process", "evaluation", "assessment", "hiring process"]:
        idx = lowered.find(marker)
        if idx >= 0:
            chunk = jd_text[idx : idx + 280].strip()
            if chunk:
                evaluation_policy = chunk
                break

    output = {
        "role_title": role_title,
        "level": level,
        "must_haves": must_haves,
        "nice_to_haves": nice_to_haves,
        "tech_stack": tech_stack,
        "focus_areas": focus_areas,
        "evaluation_policy": evaluation_policy,
        "extraction_confidence": {
            "role_title": 0.55 if role_title else 0.2,
            "level": 0.45 if level else 0.2,
            "must_haves": 0.45 if must_haves else 0.2,
            "nice_to_haves": 0.2,
            "tech_stack": 0.4 if tech_stack else 0.2,
            "focus_areas": 0.4 if focus_areas else 0.2,
            "evaluation_policy": 0.35 if evaluation_policy else 0.2,
            "overall": 0.35,
        },
        "missing_fields": [],
    }
    return output


def extract_position_details(jd_text: str) -> tuple[dict[str, Any], bool, list[str]]:
    warnings: list[str] = []
    used_llm = False
    extracted: dict[str, Any]

    if settings.openai_api_key:
        try:
            extracted = _llm_extract(jd_text)
            used_llm = True
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as exc:
            logging.exception("llm extraction failed; falling back to heuristic parser")
            warnings.append(f"LLM extraction unavailable: {exc}")
            extracted = _heuristic_extract(jd_text)
    else:
        warnings.append("OPENAI_API_KEY not configured; used heuristic extraction")
        extracted = _heuristic_extract(jd_text)

    normalized = _normalize_position_payload({**extracted, "jd_text": jd_text})
    return normalized, used_llm, warnings
