from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import api_server
from app import candidates_service


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "demo", "password": "demo-pass"},
    )
    assert response.status_code == 200


def test_candidates_create_list_update(tmp_path: Path, monkeypatch) -> None:
    candidates_file = tmp_path / "candidates.json"

    def fake_candidates_file_path() -> Path:
        return candidates_file

    monkeypatch.setattr(candidates_service, "_candidates_file_path", fake_candidates_file_path)

    app = api_server.create_app()
    client = TestClient(app)
    _login(client)

    create = client.post(
        "/api/candidates",
        json={
            "fullName": "Jane Doe",
            "email": "jane@example.com",
            "currentTitle": "Senior SDET",
            "yearsExperience": 7,
            "keySkills": ["python", "playwright", "api/ui testing"],
            "keyProjectHighlights": ["Led test automation migration"],
            "candidateContext": "Strong QA automation profile",
            "cvMetadata": {
                "originalName": "jane_cv.docx",
                "storedName": "abc_jane_cv.docx",
                "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size": 1024,
            },
            "screeningCache": {"position-1": {"score": 0.81}},
        },
    )
    assert create.status_code == 200
    created = create.json()
    assert created["id"]
    assert created["keySkills"]

    listing = client.get("/api/candidates")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1

    update = client.put(
        f"/api/candidates/{created['id']}",
        json={
            "fullName": "Jane Doe",
            "email": "jane.doe@example.com",
            "currentTitle": "Staff SDET",
            "yearsExperience": 8,
            "keySkills": ["python", "playwright", "k8s"],
            "keyProjectHighlights": ["Scaled test infra"],
            "candidateContext": "Updated context",
            "cvMetadata": created.get("cvMetadata"),
            "screeningCache": {"position-1": {"score": 0.89}},
        },
    )
    assert update.status_code == 200
    updated = update.json()
    assert updated["email"] == "jane.doe@example.com"
    assert updated["currentTitle"] == "Staff SDET"

    deleted = client.delete(f"/api/candidates/{created['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True

    missing = client.get(f"/api/candidates/{created['id']}")
    assert missing.status_code == 404


def test_candidates_extract_from_text(monkeypatch) -> None:
    def fake_extract(cv_text: str):
        return (
            {
                "fullName": "Sarita",
                "email": "sarita@example.com",
                "currentTitle": "Frontend Engineer",
                "yearsExperience": 6,
                "keySkills": ["React", "JavaScript", "GraphQL"],
                "keyProjectHighlights": ["Built micro frontend platform"],
                "candidateContext": "Strong frontend profile",
                "cvMetadata": None,
                "screeningCache": None,
            },
            False,
            ["OPENAI_API_KEY not configured; used heuristic extraction"],
        )

    monkeypatch.setattr(api_server, "extract_candidate_details", fake_extract)
    app = api_server.create_app()
    client = TestClient(app)
    _login(client)

    extract = client.post(
        "/api/candidates/extract",
        data={"cv_text": "Sarita\nFrontend Engineer\nReact JavaScript GraphQL"},
    )
    assert extract.status_code == 200
    body = extract.json()
    assert body["fullName"] == "Sarita"
    assert body["used_llm"] is False
