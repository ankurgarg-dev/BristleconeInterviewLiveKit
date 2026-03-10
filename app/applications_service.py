from __future__ import annotations

import json
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import certifi

from app.positions_service import CANONICAL_SKILL_MAP
from shared.config import settings

_applications_lock = threading.Lock()

APPLICATION_STATUSES = {
    "applied",
    "screened",
    "shortlisted",
    "interview_scheduled",
    "interview_completed",
    "rejected",
    "hired",
}

INTERVIEW_AGENTS = {"assistant", "support", "interviewer", "realtime", "observer"}


def _applications_file_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "applications.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = text.strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _canonicalize_skill(raw: str) -> str:
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    if not text:
        return ""
    compact = re.sub(r"[^a-z0-9#+./\s]", "", text.casefold())
    compact = re.sub(r"\s+", " ", compact).strip()
    canonical = CANONICAL_SKILL_MAP.get(compact)
    if canonical:
        return canonical
    return text


def _normalize_skills(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        chunks = re.split(r"[,/\n;]", values)
    elif isinstance(values, list):
        chunks = values
    else:
        return []
    output: list[str] = []
    seen: set[str] = set()
    for raw in chunks:
        skill = _canonicalize_skill(str(raw or ""))
        if not skill:
            continue
        key = skill.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(skill)
    return output


def _normalize_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in APPLICATION_STATUSES:
        return status
    return "applied"


def _to_float_0_1(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num < 0:
        num = 0.0
    if num > 1:
        num = 1.0
    return round(num, 3)


def _to_float_0_10(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num < 0:
        num = 0.0
    if num > 10:
        num = 10.0
    return round(num, 2)


def _to_float_0_100(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num < 0:
        num = 0.0
    if num > 100:
        num = 100.0
    return round(num, 2)


def _safe_datetime_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_dimension_scores(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    mapping = {
        "technical_skills_match": "technical_skills_match",
        "relevant_experience": "relevant_experience",
        "domain_knowledge": "domain_knowledge",
        "tools_technologies": "tools_technologies",
        "education_certifications": "education_certifications",
        "overall_fit": "overall_fit",
    }
    for key, target in mapping.items():
        score = _to_float_0_10(raw.get(key))
        if score is not None:
            out[target] = score
    return out


def _normalize_simple_list(raw: Any, *, max_items: int = 10, max_len: int = 220) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        out.append(text[:max_len])
        if len(out) >= max_items:
            break
    return out


def _normalize_job_summary(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        "required_skills": _normalize_skills(raw.get("required_skills")),
        "preferred_skills": _normalize_skills(raw.get("preferred_skills")),
        "experience_required": str(raw.get("experience_required") or "").strip(),
        "domain": str(raw.get("domain") or "").strip(),
        "tools_technologies": _normalize_skills(raw.get("tools_technologies")),
        "education_requirements": str(raw.get("education_requirements") or "").strip(),
        "soft_skills": _normalize_simple_list(raw.get("soft_skills"), max_items=10, max_len=80),
    }


def _normalize_candidate_summary(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    years = _to_float_0_100(raw.get("years_of_experience"))
    if years is not None and years > 60:
        years = 60.0
    return {
        "years_of_experience": years,
        "core_skills": _normalize_skills(raw.get("core_skills")),
        "key_achievements": _normalize_simple_list(raw.get("key_achievements"), max_items=10),
        "relevant_tools": _normalize_skills(raw.get("relevant_tools")),
    }


def _normalize_screening(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    overall_percent = _to_float_0_100(raw.get("overall_match_score"))
    score = _to_float_0_1(raw.get("score"))
    if score is None and overall_percent is not None:
        score = round(overall_percent / 100.0, 3)
    if overall_percent is None and score is not None:
        overall_percent = round(score * 100.0, 2)

    match_analysis = raw.get("match_analysis") if isinstance(raw.get("match_analysis"), dict) else {}
    strong_matches = _normalize_simple_list(match_analysis.get("strong_matches"), max_items=20)
    partial_matches = _normalize_simple_list(match_analysis.get("partial_matches"), max_items=20)
    missing_weak_areas = _normalize_simple_list(match_analysis.get("missing_or_weak_areas"), max_items=20)

    matched = _normalize_skills(raw.get("matched_skills")) or _normalize_skills(strong_matches)
    missing = _normalize_skills(raw.get("missing_skills")) or _normalize_skills(missing_weak_areas)
    strengths = [str(item).strip() for item in (raw.get("strengths") or []) if str(item).strip()]
    risks = [str(item).strip() for item in (raw.get("risks") or []) if str(item).strip()]
    if not strengths:
        strengths = strong_matches[:6]
    if not risks:
        risks = missing_weak_areas[:6]

    score_breakdown = _normalize_dimension_scores(raw.get("score_breakdown") or raw.get("scores"))
    hiring_reasoning = _normalize_simple_list(raw.get("hiring_reasoning"), max_items=7)
    interview_questions = _normalize_simple_list(raw.get("interview_questions"), max_items=7)
    hiring_recommendation = str(raw.get("hiring_recommendation") or "").strip() or "Borderline"
    job_requirements_summary = _normalize_job_summary(raw.get("job_requirements_summary"))
    candidate_profile_summary = _normalize_candidate_summary(raw.get("candidate_profile_summary"))

    report = str(raw.get("report") or "").strip()
    if not report:
        report_lines = [
            "JOB REQUIREMENTS SUMMARY",
            f"- Required Skills: {', '.join(job_requirements_summary.get('required_skills') or []) or '-'}",
            f"- Preferred Skills: {', '.join(job_requirements_summary.get('preferred_skills') or []) or '-'}",
            f"- Experience Required: {job_requirements_summary.get('experience_required') or '-'}",
            f"- Domain: {job_requirements_summary.get('domain') or '-'}",
            "",
            "CANDIDATE PROFILE SUMMARY",
            f"- Years of experience: {candidate_profile_summary.get('years_of_experience') if candidate_profile_summary.get('years_of_experience') is not None else '-'}",
            f"- Core skills: {', '.join(candidate_profile_summary.get('core_skills') or []) or '-'}",
            "",
            "MATCH ANALYSIS",
            f"Strong Matches: {', '.join(strong_matches) or '-'}",
            f"Partial Matches: {', '.join(partial_matches) or '-'}",
            f"Missing or Weak Areas: {', '.join(missing_weak_areas) or '-'}",
            "",
            "SCORING",
            f"Overall Match Score: {overall_percent if overall_percent is not None else '-'}%",
            "",
            "HIRING RECOMMENDATION",
            hiring_recommendation,
        ]
        report = "\n".join(report_lines).strip()

    normalized = {
        "score": score,
        "overall_match_score": overall_percent,
        "justification": str(raw.get("justification") or "").strip(),
        "matched_skills": matched,
        "missing_skills": missing,
        "strengths": strengths[:6],
        "risks": risks[:6],
        "job_requirements_summary": job_requirements_summary,
        "candidate_profile_summary": candidate_profile_summary,
        "match_analysis": {
            "strong_matches": strong_matches,
            "partial_matches": partial_matches,
            "missing_or_weak_areas": missing_weak_areas,
        },
        "score_breakdown": score_breakdown,
        "hiring_recommendation": hiring_recommendation,
        "hiring_reasoning": hiring_reasoning,
        "interview_questions": interview_questions,
        "report": report,
        "used_llm": bool(raw.get("used_llm")),
        "updated_at": str(raw.get("updated_at") or _now_iso()),
    }
    if (
        normalized["score"] is None
        and not normalized["justification"]
        and not normalized["matched_skills"]
        and not normalized["missing_skills"]
        and not normalized["report"]
    ):
        return None
    return normalized


def _slug(value: str, fallback: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
    return clean[:30] or fallback


def _normalize_interview(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    room = str(raw.get("room") or "").strip()
    if not room:
        return None
    return {
        "room": room,
        "scheduled_for": _safe_datetime_text(raw.get("scheduled_for")),
        "stage": str(raw.get("stage") or "technical_screen").strip() or "technical_screen",
        "status": str(raw.get("status") or "scheduled").strip() or "scheduled",
        "agent": _normalize_interview_agent(raw.get("agent")),
        "notes": str(raw.get("notes") or "").strip(),
        "updated_at": str(raw.get("updated_at") or _now_iso()),
    }


def _normalize_interviews(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        normalized = _normalize_interview(item)
        if normalized is None:
            continue
        out.append(normalized)
    out.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    return out


def _normalize_interview_agent(value: Any) -> str:
    agent = str(value or "").strip().lower()
    if agent in INTERVIEW_AGENTS:
        return agent
    return "interviewer"


def _position_snapshot(position: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(position, dict):
        return None
    return {
        "position_id": str(position.get("position_id") or ""),
        "role_title": str(position.get("role_title") or ""),
        "level": str(position.get("level") or ""),
        "must_haves": _normalize_skills(position.get("must_haves")),
        "tech_stack": _normalize_skills(position.get("tech_stack")),
    }


def _candidate_snapshot(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    years = candidate.get("yearsExperience")
    return {
        "candidate_id": str(candidate.get("id") or candidate.get("candidate_id") or ""),
        "fullName": str(candidate.get("fullName") or ""),
        "email": str(candidate.get("email") or ""),
        "currentTitle": str(candidate.get("currentTitle") or ""),
        "yearsExperience": float(years) if isinstance(years, (int, float)) else None,
        "keySkills": _normalize_skills(candidate.get("keySkills")),
    }


def _normalize_application_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "position_id": str(payload.get("position_id") or "").strip(),
        "candidate_id": str(payload.get("candidate_id") or "").strip(),
        "status": _normalize_status(payload.get("status")),
        "source": str(payload.get("source") or "manual").strip() or "manual",
        "notes": str(payload.get("notes") or "").strip(),
        "screening": _normalize_screening(payload.get("screening")),
        "interview": _normalize_interview(payload.get("interview")),
        "interviews": _normalize_interviews(payload.get("interviews")),
        "position_snapshot": _position_snapshot(payload.get("position_snapshot")),
        "candidate_snapshot": _candidate_snapshot(payload.get("candidate_snapshot")),
    }


def _write_applications(rows: list[dict[str, Any]]) -> None:
    path = _applications_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(rows, fp, ensure_ascii=False, indent=2)


def load_applications() -> list[dict[str, Any]]:
    path = _applications_file_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except json.JSONDecodeError:
        logging.exception("applications file is corrupted")
        return []
    if not isinstance(data, list):
        return []

    cleaned: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        base = _normalize_application_payload(row)
        base["application_id"] = str(row.get("application_id") or "")
        base["created_by"] = str(row.get("created_by") or "")
        base["created_at"] = str(row.get("created_at") or "")
        base["updated_at"] = str(row.get("updated_at") or "")
        try:
            base["version"] = int(row.get("version") or 1)
        except (TypeError, ValueError):
            base["version"] = 1
        cleaned.append(base)

    cleaned.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return cleaned


def get_application(application_id: str) -> dict[str, Any] | None:
    for row in load_applications():
        if row.get("application_id") == application_id:
            return row
    return None


def create_application(
    payload: dict[str, Any],
    *,
    created_by: str,
    position: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _now_iso()
    with _applications_lock:
        rows = load_applications()
        normalized = _normalize_application_payload(payload)
        normalized["application_id"] = str(uuid.uuid4())
        normalized["created_by"] = created_by
        normalized["created_at"] = now
        normalized["updated_at"] = now
        normalized["version"] = 1
        if normalized.get("screening") is None:
            raise ValueError("screening is required before creating an application")
        if normalized.get("interview") and not normalized.get("interviews"):
            normalized["interviews"] = [normalized["interview"]]
        if position:
            normalized["position_snapshot"] = _position_snapshot(position)
        if candidate:
            normalized["candidate_snapshot"] = _candidate_snapshot(candidate)
        rows.append(normalized)
        rows.sort(key=lambda item: item["updated_at"], reverse=True)
        _write_applications(rows)
    return normalized


def update_application(
    application_id: str,
    payload: dict[str, Any],
    *,
    position: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    now = _now_iso()
    with _applications_lock:
        rows = load_applications()
        for idx, row in enumerate(rows):
            if row.get("application_id") != application_id:
                continue
            merged = _normalize_application_payload(payload)
            merged["application_id"] = row["application_id"]
            merged["created_by"] = row.get("created_by", "")
            merged["created_at"] = row.get("created_at", now)
            merged["updated_at"] = now
            merged["version"] = int(row.get("version", 1)) + 1
            if merged.get("screening") is None and row.get("screening"):
                merged["screening"] = row.get("screening")
            if not merged.get("interviews") and row.get("interviews"):
                merged["interviews"] = _normalize_interviews(row.get("interviews"))
            elif not merged.get("interviews") and row.get("interview"):
                merged["interviews"] = [row.get("interview")]
            if merged.get("interview") and not merged.get("interviews"):
                merged["interviews"] = [merged["interview"]]
            if position:
                merged["position_snapshot"] = _position_snapshot(position)
            elif row.get("position_snapshot"):
                merged["position_snapshot"] = row.get("position_snapshot")
            if candidate:
                merged["candidate_snapshot"] = _candidate_snapshot(candidate)
            elif row.get("candidate_snapshot"):
                merged["candidate_snapshot"] = row.get("candidate_snapshot")
            rows[idx] = merged
            rows.sort(key=lambda item: item["updated_at"], reverse=True)
            _write_applications(rows)
            return merged
    return None


def delete_application(application_id: str) -> bool:
    with _applications_lock:
        rows = load_applications()
        remaining = [row for row in rows if row.get("application_id") != application_id]
        if len(remaining) == len(rows):
            return False
        _write_applications(remaining)
    return True


def _required_skills(position: dict[str, Any]) -> list[str]:
    merged = []
    merged.extend(position.get("must_haves") or [])
    merged.extend(position.get("tech_stack") or [])
    return _normalize_skills(merged)


def _preferred_skills(position: dict[str, Any]) -> list[str]:
    return _normalize_skills(position.get("nice_to_haves") or [])


def _candidate_skills(candidate: dict[str, Any]) -> list[str]:
    return _normalize_skills(candidate.get("keySkills") or [])


def _required_years_from_text(jd_text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+|plus)?\s*years", jd_text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _heuristic_screen_application(position: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    required = _required_skills(position)
    preferred = _preferred_skills(position)
    candidate_skills = _candidate_skills(candidate)

    required_set = {skill.casefold() for skill in required}
    preferred_set = {skill.casefold() for skill in preferred}
    candidate_set = {skill.casefold() for skill in candidate_skills}

    matched = [skill for skill in required if skill.casefold() in candidate_set]
    missing = [skill for skill in required if skill.casefold() not in candidate_set]
    preferred_hit = [skill for skill in preferred if skill.casefold() in candidate_set]

    required_ratio = len(matched) / len(required_set) if required_set else 0.5
    preferred_ratio = len(preferred_hit) / len(preferred_set) if preferred_set else 0.5

    years_required = _required_years_from_text(str(position.get("jd_text") or ""))
    years_have = candidate.get("yearsExperience")
    exp_ratio = 0.5
    if isinstance(years_required, (int, float)) and years_required > 0 and isinstance(years_have, (int, float)):
        exp_ratio = min(1.0, max(0.0, float(years_have) / float(years_required)))

    score = 0.05 + (0.65 * required_ratio) + (0.16 * preferred_ratio) + (0.14 * exp_ratio)
    if len(matched) == 0 and required:
        # Hard penalty for zero required overlap to avoid false-positive high scores.
        score = min(score, 0.28)
    score = round(max(0.0, min(1.0, score)), 3)
    overall_percent = round(score * 100.0, 2)

    reasons: list[str] = []
    if matched:
        reasons.append(f"Matched required skills: {', '.join(matched[:6])}.")
    if missing:
        reasons.append(f"Missing required coverage on: {', '.join(missing[:6])}.")
    if preferred_hit:
        reasons.append(f"Also aligns with preferred skills: {', '.join(preferred_hit[:4])}.")
    if isinstance(years_have, (int, float)):
        years_text = f"{float(years_have):g}"
        if years_required:
            reasons.append(f"Experience signals: {years_text} years vs ~{years_required:g} years requirement.")
        else:
            reasons.append(f"Experience signals: {years_text} years reported.")
    justification = " ".join(reasons).strip() or "Limited overlap signal available from current profile fields."

    strengths = []
    if matched:
        strengths.append(f"Strong overlap in core skills ({len(matched)} of {len(required)} required tags matched).")
    if preferred_hit:
        strengths.append(f"Additional alignment with preferred skills ({', '.join(preferred_hit[:3])}).")

    risks = []
    if missing:
        risks.append(f"Skill gaps on required tags: {', '.join(missing[:4])}.")
    if isinstance(years_required, (int, float)) and isinstance(years_have, (int, float)) and years_have < years_required:
        risks.append("Years-of-experience signal is below the role requirement.")

    return {
        "score": score,
        "overall_match_score": overall_percent,
        "justification": justification,
        "matched_skills": matched,
        "missing_skills": missing,
        "strengths": strengths,
        "risks": risks,
        "job_requirements_summary": {
            "required_skills": required,
            "preferred_skills": preferred,
            "experience_required": f"{years_required:g} years" if years_required else "",
            "domain": str(position.get("focus_areas") or ""),
            "tools_technologies": _normalize_skills(position.get("tech_stack")),
            "education_requirements": "",
            "soft_skills": [],
        },
        "candidate_profile_summary": {
            "years_of_experience": float(years_have) if isinstance(years_have, (int, float)) else None,
            "core_skills": candidate_skills,
            "key_achievements": _normalize_simple_list(candidate.get("keyProjectHighlights"), max_items=5),
            "relevant_tools": candidate_skills,
        },
        "match_analysis": {
            "strong_matches": strengths[:5],
            "partial_matches": [],
            "missing_or_weak_areas": risks[:5],
        },
        "score_breakdown": {
            "technical_skills_match": round(required_ratio * 10, 2),
            "relevant_experience": round(exp_ratio * 10, 2),
            "domain_knowledge": round((preferred_ratio * 7 + required_ratio * 3), 2),
            "tools_technologies": round((required_ratio * 8 + preferred_ratio * 2) * 10 / 10, 2),
            "education_certifications": 5.0,
            "overall_fit": round(score * 10, 2),
        },
        "hiring_recommendation": "Reject" if score < 0.4 else "Borderline" if score < 0.65 else "Hire",
        "hiring_reasoning": _normalize_simple_list(strengths + risks, max_items=5),
        "interview_questions": [
            "Walk through one project where you used the core required stack end-to-end.",
            "What trade-offs did you make in architecture decisions for scalability and reliability?",
            "How do you validate correctness and performance under production-like load?",
            "Describe your experience with cloud deployment and observability for this stack.",
            "Which requirements in this role are your strongest and weakest, and why?",
        ],
    }


def _llm_screen_application(position: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    jd_text = str(position.get("jd_text") or "").strip()
    if not jd_text:
        jd_text = json.dumps(position, ensure_ascii=False)

    cv_text = str(candidate.get("cvTextSummary") or "").strip()
    if not cv_text:
        cv_text = "\n".join(
            [
                str(candidate.get("fullName") or ""),
                str(candidate.get("currentTitle") or ""),
                f"Years: {candidate.get('yearsExperience')}",
                f"Skills: {', '.join(candidate.get('keySkills') or [])}",
                f"Highlights: {'; '.join(candidate.get('keyProjectHighlights') or [])}",
                str(candidate.get("candidateContext") or ""),
            ]
        ).strip()

    recruiter_prompt_template = """You are an expert technical recruiter and hiring manager.

Your task is to evaluate how well a candidate's CV matches a given Job Description (JD). Be objective, analytical, and evidence-based.

INPUTS
1. Job Description (JD)
2. Candidate CV/Resume

EVALUATION INSTRUCTIONS

1. Extract the key requirements from the Job Description:
   - Required skills
   - Preferred skills
   - Years of experience
   - Domain experience
   - Tools/technologies
   - Education requirements
   - Soft skills

2. Analyze the candidate CV and identify:
   - Matching skills
   - Relevant experience
   - Achievements and impact
   - Tools and technologies used
   - Domain expertise

3. Compare the CV against the JD and determine:
   - Strong matches
   - Partial matches
   - Missing requirements
   - Overqualification (if any)

4. Evaluate the candidate on the following dimensions (score each out of 10):
   - Technical Skills Match
   - Relevant Experience
   - Domain Knowledge
   - Tools & Technologies
   - Education/Certifications
   - Overall Fit

5. Calculate an Overall Match Score (0–100%).

OUTPUT FORMAT

Provide results in the following structured format:

------------------------------------------------

JOB REQUIREMENTS SUMMARY
- Required Skills:
- Preferred Skills:
- Experience Required:
- Domain:

CANDIDATE PROFILE SUMMARY
- Years of experience:
- Core skills:
- Key achievements:
- Relevant tools:

MATCH ANALYSIS

Strong Matches
-
-
-

Partial Matches
-
-
-

Missing or Weak Areas
-
-
-

SCORING

Technical Skills Match: X/10
Relevant Experience: X/10
Domain Knowledge: X/10
Tools & Technologies: X/10
Education/Certifications: X/10

Overall Match Score: XX%

HIRING RECOMMENDATION
Choose one:
- Strong Hire
- Hire
- Borderline
- Reject

Explain the reasoning clearly in 3–5 bullet points.

INTERVIEW QUESTIONS TO VALIDATE THE CANDIDATE
Provide 5 targeted questions to verify critical skills.

------------------------------------------------

JOB DESCRIPTION:
[PASTE JD HERE]

CANDIDATE CV:
[PASTE CV HERE]"""
    filled_prompt = recruiter_prompt_template.replace("[PASTE JD HERE]", jd_text[:35000]).replace("[PASTE CV HERE]", cv_text[:35000])

    schema = {
        "name": "application_screening",
        "schema": {
            "type": "object",
            "properties": {
                "job_requirements_summary": {
                    "type": "object",
                    "properties": {
                        "required_skills": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 30},
                        "preferred_skills": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 30},
                        "experience_required": {"type": "string", "maxLength": 120},
                        "domain": {"type": "string", "maxLength": 160},
                        "tools_technologies": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 30},
                        "education_requirements": {"type": "string", "maxLength": 180},
                        "soft_skills": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 15},
                    },
                    "required": ["required_skills", "preferred_skills", "experience_required", "domain", "tools_technologies", "education_requirements", "soft_skills"],
                    "additionalProperties": False,
                },
                "candidate_profile_summary": {
                    "type": "object",
                    "properties": {
                        "years_of_experience": {"type": ["number", "null"]},
                        "core_skills": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 30},
                        "key_achievements": {"type": "array", "items": {"type": "string", "maxLength": 220}, "maxItems": 15},
                        "relevant_tools": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 30},
                    },
                    "required": ["years_of_experience", "core_skills", "key_achievements", "relevant_tools"],
                    "additionalProperties": False,
                },
                "match_analysis": {
                    "type": "object",
                    "properties": {
                        "strong_matches": {"type": "array", "items": {"type": "string", "maxLength": 220}, "maxItems": 20},
                        "partial_matches": {"type": "array", "items": {"type": "string", "maxLength": 220}, "maxItems": 20},
                        "missing_or_weak_areas": {"type": "array", "items": {"type": "string", "maxLength": 220}, "maxItems": 20},
                    },
                    "required": ["strong_matches", "partial_matches", "missing_or_weak_areas"],
                    "additionalProperties": False,
                },
                "score_breakdown": {
                    "type": "object",
                    "properties": {
                        "technical_skills_match": {"type": "number", "minimum": 0, "maximum": 10},
                        "relevant_experience": {"type": "number", "minimum": 0, "maximum": 10},
                        "domain_knowledge": {"type": "number", "minimum": 0, "maximum": 10},
                        "tools_technologies": {"type": "number", "minimum": 0, "maximum": 10},
                        "education_certifications": {"type": "number", "minimum": 0, "maximum": 10},
                        "overall_fit": {"type": "number", "minimum": 0, "maximum": 10},
                    },
                    "required": ["technical_skills_match", "relevant_experience", "domain_knowledge", "tools_technologies", "education_certifications", "overall_fit"],
                    "additionalProperties": False,
                },
                "overall_match_score": {"type": "number", "minimum": 0, "maximum": 100},
                "hiring_recommendation": {"type": "string", "enum": ["Strong Hire", "Hire", "Borderline", "Reject"]},
                "hiring_reasoning": {"type": "array", "items": {"type": "string", "maxLength": 220}, "minItems": 3, "maxItems": 7},
                "interview_questions": {"type": "array", "items": {"type": "string", "maxLength": 220}, "minItems": 5, "maxItems": 7},
                "justification": {"type": "string", "maxLength": 1200},
                "report": {"type": "string", "maxLength": 12000},
            },
            "required": [
                "job_requirements_summary",
                "candidate_profile_summary",
                "match_analysis",
                "score_breakdown",
                "overall_match_score",
                "hiring_recommendation",
                "hiring_reasoning",
                "interview_questions",
                "justification",
                "report",
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
                    "You are screening a candidate against a job position. "
                    "Follow the provided template exactly and stay evidence-based."
                ),
            },
            {
                "role": "user",
                "content": filled_prompt,
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
        with urllib.request.urlopen(req, timeout=40, context=ssl_ctx) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise ValueError(f"OpenAI application screening failed: {exc.code} {detail[:300]}") from exc
    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    parsed = _extract_json_object(content)
    if parsed is None:
        raise ValueError("unable to parse screening response")
    return parsed


def screen_application(position: dict[str, Any], candidate: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    warnings: list[str] = []
    used_llm = False

    if settings.openai_api_key:
        try:
            screened = _llm_screen_application(position, candidate)
            used_llm = True
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            logging.exception("application llm screening failed; using heuristic")
            warnings.append(f"LLM screening unavailable: {exc}")
            screened = _heuristic_screen_application(position, candidate)
    else:
        warnings.append("OPENAI_API_KEY not configured; used heuristic screening")
        screened = _heuristic_screen_application(position, candidate)

    normalized = _normalize_screening(screened) or {}
    normalized["used_llm"] = used_llm
    normalized["updated_at"] = _now_iso()
    return normalized, used_llm, warnings


def build_interview(
    *,
    application_id: str,
    position_title: str,
    candidate_name: str,
    scheduled_for: str | None,
    stage: str | None,
    agent: str | None,
    notes: str | None = None,
) -> dict[str, Any]:
    role_slug = _slug(position_title, "role")
    candidate_slug = _slug(candidate_name, "candidate")
    app_slug = _slug(application_id[:8], "app")
    attempt = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    suffix = uuid.uuid4().hex[:6]
    room = f"interview-{role_slug}-{candidate_slug}-{app_slug}-{attempt}-{suffix}"
    normalized_agent = _normalize_interview_agent(agent)
    if normalized_agent == "observer" and not room.endswith("-observer"):
        room = f"{room}-observer"
    return {
        "room": room,
        "scheduled_for": _safe_datetime_text(scheduled_for),
        "stage": str(stage or "technical_screen").strip() or "technical_screen",
        "status": "scheduled",
        "agent": normalized_agent,
        "notes": str(notes or "").strip(),
        "updated_at": _now_iso(),
    }
