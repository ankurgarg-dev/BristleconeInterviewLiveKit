from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import api_server
from app import applications_service
from app import candidates_service
from app import positions_service


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "demo", "password": "demo-pass"},
    )
    assert response.status_code == 200


def test_applications_crud_screen_schedule_and_interviews(tmp_path: Path, monkeypatch) -> None:
    positions_file = tmp_path / "positions.json"
    candidates_file = tmp_path / "candidates.json"
    applications_file = tmp_path / "applications.json"

    monkeypatch.setattr(positions_service, "_positions_file_path", lambda: positions_file)
    monkeypatch.setattr(candidates_service, "_candidates_file_path", lambda: candidates_file)
    monkeypatch.setattr(applications_service, "_applications_file_path", lambda: applications_file)

    app = api_server.create_app()
    client = TestClient(app)
    _login(client)

    create_position = client.post(
        "/api/positions",
        json={
            "role_title": "Senior Backend Engineer",
            "jd_text": "Need Python, FastAPI, AWS and 6+ years experience",
            "level": "Senior",
            "must_haves": ["Python", "FastAPI", "AWS"],
            "nice_to_haves": ["Kubernetes"],
            "tech_stack": ["Python"],
            "focus_areas": ["Backend"],
            "evaluation_policy": "",
            "extraction_confidence": {"overall": 0.9},
            "missing_fields": [],
        },
    )
    assert create_position.status_code == 200
    position_id = create_position.json()["position_id"]

    create_candidate = client.post(
        "/api/candidates",
        json={
            "fullName": "Jane Doe",
            "email": "jane@example.com",
            "currentTitle": "Backend Engineer",
            "yearsExperience": 7,
            "keySkills": ["Python", "FastAPI", "Docker"],
            "keyProjectHighlights": ["Built internal platform APIs"],
            "candidateContext": "Strong backend profile",
            "cvTextSummary": "Summary",
            "cvMetadata": None,
            "screeningCache": None,
        },
    )
    assert create_candidate.status_code == 200
    candidate_id = create_candidate.json()["id"]

    create_application = client.post(
        "/api/applications",
        json={
            "position_id": position_id,
            "candidate_id": candidate_id,
            "status": "applied",
            "source": "manual",
            "notes": "First review pending",
            "screening": {
                "score": 0.55,
                "overall_match_score": 55,
                "justification": "Initial screening result.",
                "matched_skills": ["Python"],
                "missing_skills": ["AWS"],
                "strengths": ["Python match"],
                "risks": ["Cloud gap"],
                "score_breakdown": {
                    "technical_skills_match": 6,
                    "relevant_experience": 5,
                    "domain_knowledge": 5,
                    "tools_technologies": 5,
                    "education_certifications": 5,
                    "overall_fit": 5.5,
                },
                "hiring_recommendation": "Borderline",
                "hiring_reasoning": ["Needs deeper AWS exposure"],
                "interview_questions": ["Describe your AWS architecture experience."],
                "report": "Initial report",
            },
            "interview": None,
            "position_snapshot": None,
            "candidate_snapshot": None,
        },
    )
    assert create_application.status_code == 200
    created = create_application.json()
    assert created["application_id"]
    assert created["position_snapshot"]["role_title"] == "Senior Backend Engineer"
    assert created["candidate_snapshot"]["fullName"] == "Jane Doe"

    application_id = created["application_id"]

    update_application = client.put(
        f"/api/applications/{application_id}",
        json={
            "position_id": position_id,
            "candidate_id": candidate_id,
            "status": "shortlisted",
            "source": "manual",
            "notes": "Moved to shortlist",
            "screening": None,
            "interview": None,
            "position_snapshot": created["position_snapshot"],
            "candidate_snapshot": created["candidate_snapshot"],
        },
    )
    assert update_application.status_code == 200
    assert update_application.json()["status"] == "shortlisted"

    def fake_screen(position: dict, candidate: dict):
        assert position["position_id"] == position_id
        assert candidate["id"] == candidate_id
        return (
            {
                "score": 0.82,
                "justification": "Good alignment on core backend skills with one cloud gap.",
                "matched_skills": ["Python", "FastAPI"],
                "missing_skills": ["AWS"],
                "strengths": ["Strong backend fundamentals"],
                "risks": ["Needs AWS depth"],
                "used_llm": False,
                "updated_at": "2026-03-09T00:00:00+00:00",
            },
            False,
            ["OPENAI_API_KEY not configured; used heuristic screening"],
        )

    monkeypatch.setattr(api_server, "screen_application", fake_screen)

    screen = client.post(f"/api/applications/{application_id}/screen")
    assert screen.status_code == 200
    screen_body = screen.json()
    assert screen_body["application"]["screening"]["score"] == 0.82
    assert screen_body["application"]["status"] == "screened"
    assert screen_body["used_llm"] is False

    schedule = client.post(
        f"/api/applications/{application_id}/schedule-interview",
        json={
            "scheduled_for": "2026-03-10T10:30:00Z",
            "stage": "technical_screen",
            "notes": "Panel with backend leads",
        },
    )
    assert schedule.status_code == 200
    schedule_body = schedule.json()
    assert schedule_body["status"] == "interview_scheduled"
    assert schedule_body["interview"]["room"].startswith("interview-")

    interviews = client.get("/api/interviews")
    assert interviews.status_code == 200
    interview_rows = interviews.json()
    assert len(interview_rows) == 1
    assert interview_rows[0]["application_id"] == application_id
    assert interview_rows[0]["interview"]["room"]


def test_application_create_requires_screening(tmp_path: Path, monkeypatch) -> None:
    positions_file = tmp_path / "positions.json"
    candidates_file = tmp_path / "candidates.json"
    applications_file = tmp_path / "applications.json"

    monkeypatch.setattr(positions_service, "_positions_file_path", lambda: positions_file)
    monkeypatch.setattr(candidates_service, "_candidates_file_path", lambda: candidates_file)
    monkeypatch.setattr(applications_service, "_applications_file_path", lambda: applications_file)

    app = api_server.create_app()
    client = TestClient(app)
    _login(client)

    position_id = client.post(
        "/api/positions",
        json={
            "role_title": "Python Engineer",
            "jd_text": "Need Python",
            "level": "Senior",
            "must_haves": ["Python"],
            "nice_to_haves": [],
            "tech_stack": ["Python"],
            "focus_areas": ["Backend"],
            "evaluation_policy": "",
            "extraction_confidence": {"overall": 1.0},
            "missing_fields": [],
        },
    ).json()["position_id"]
    candidate_id = client.post(
        "/api/candidates",
        json={
            "fullName": "John",
            "email": "john@example.com",
            "currentTitle": "Engineer",
            "yearsExperience": 4,
            "keySkills": ["Java"],
            "keyProjectHighlights": [],
            "candidateContext": "",
            "cvTextSummary": "",
            "cvMetadata": None,
            "screeningCache": None,
        },
    ).json()["id"]

    create = client.post(
        "/api/applications",
        json={
            "position_id": position_id,
            "candidate_id": candidate_id,
            "source": "manual",
            "notes": "",
            "screening": None,
        },
    )
    assert create.status_code == 400
    assert "screening" in create.json()["detail"]
