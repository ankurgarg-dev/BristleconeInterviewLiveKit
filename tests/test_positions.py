from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import api_server
from app import positions_service


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "demo", "password": "demo-pass"},
    )
    assert response.status_code == 200


def test_positions_create_list_update(tmp_path: Path, monkeypatch) -> None:
    positions_file = tmp_path / "positions.json"

    def fake_positions_file_path() -> Path:
        return positions_file

    monkeypatch.setattr(positions_service, "_positions_file_path", fake_positions_file_path)

    app = api_server.create_app()
    client = TestClient(app)
    _login(client)

    create = client.post(
        "/api/positions",
        json={
            "role_title": "Senior Backend Engineer",
            "jd_text": "Build APIs and own platform reliability",
            "level": "Senior",
            "must_haves": ["Python", "FastAPI"],
            "nice_to_haves": ["Kubernetes"],
            "tech_stack": ["Python", "Postgres"],
            "focus_areas": ["Backend", "Platform"],
            "evaluation_policy": "System design + coding + leadership round",
            "extraction_confidence": {"overall": 0.88},
            "missing_fields": [],
        },
    )
    assert create.status_code == 200
    created = create.json()
    assert created["position_id"]
    assert created["version"] == 1

    listing = client.get("/api/positions")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["role_title"] == "Senior Backend Engineer"

    updated = client.put(
        f"/api/positions/{created['position_id']}",
        json={
            "role_title": "Staff Backend Engineer",
            "jd_text": created["jd_text"],
            "level": "Staff",
            "must_haves": ["Python", "FastAPI", "Distributed Systems"],
            "nice_to_haves": ["Kubernetes"],
            "tech_stack": ["Python", "Postgres"],
            "focus_areas": ["Backend", "Architecture"],
            "evaluation_policy": "System design + coding + leadership round",
            "extraction_confidence": {"overall": 0.9},
            "missing_fields": [],
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["version"] == 2
    assert body["role_title"] == "Staff Backend Engineer"

    deleted = client.delete(f"/api/positions/{created['position_id']}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True

    missing = client.get(f"/api/positions/{created['position_id']}")
    assert missing.status_code == 404


def test_positions_extract_from_text_without_openai(monkeypatch) -> None:
    def fake_extract(jd_text: str):
        return (
            {
                "role_title": "Senior Data Engineer",
                "jd_text": jd_text,
                "level": "Senior",
                "must_haves": ["Python", "SQL", "Airflow", "AWS"],
                "nice_to_haves": [],
                "tech_stack": ["Python", "SQL", "Airflow", "AWS"],
                "focus_areas": ["data engineering"],
                "evaluation_policy": "",
                "extraction_confidence": {"overall": 0.4},
                "missing_fields": [],
            },
            False,
            ["OPENAI_API_KEY not configured; used heuristic extraction"],
        )

    monkeypatch.setattr(api_server, "extract_position_details", fake_extract)
    app = api_server.create_app()
    client = TestClient(app)
    _login(client)

    extract = client.post(
        "/api/positions/extract",
        data={
            "jd_text": "Senior Data Engineer\nMust have Python, SQL, Airflow and AWS. Focus on data engineering.",
        },
    )

    assert extract.status_code == 200
    body = extract.json()
    assert body["role_title"]
    assert body["must_haves"]
    assert body["used_llm"] is False
    assert body["warnings"]


def test_positions_canonicalize_skills(tmp_path: Path, monkeypatch) -> None:
    positions_file = tmp_path / "positions.json"

    def fake_positions_file_path() -> Path:
        return positions_file

    monkeypatch.setattr(positions_service, "_positions_file_path", fake_positions_file_path)
    app = api_server.create_app()
    client = TestClient(app)
    _login(client)

    create = client.post(
        "/api/positions",
        json={
            "role_title": "Platform Engineer",
            "jd_text": "text",
            "level": "Senior",
            "must_haves": ["python", "Py", "aws", "Amazon Web Services", "nodejs"],
            "nice_to_haves": ["Preferred: reactjs", "python", "good to have: terraform"],
            "tech_stack": ["ci cd", "postgresql", "k8s"],
            "focus_areas": ["devops", "fullstack"],
            "evaluation_policy": "",
            "extraction_confidence": {"overall": 0.5},
            "missing_fields": [],
        },
    )
    assert create.status_code == 200
    body = create.json()
    assert body["must_haves"] == ["Python", "AWS", "Node.js"]
    assert body["nice_to_haves"] == ["React", "Terraform"]
    assert body["tech_stack"] == ["CI/CD", "PostgreSQL", "Kubernetes"]
    assert body["focus_areas"] == ["DevOps", "Full Stack"]


def test_positions_skill_noise_cleanup(tmp_path: Path, monkeypatch) -> None:
    positions_file = tmp_path / "positions.json"

    def fake_positions_file_path() -> Path:
        return positions_file

    monkeypatch.setattr(positions_service, "_positions_file_path", fake_positions_file_path)
    app = api_server.create_app()
    client = TestClient(app)
    _login(client)

    create = client.post(
        "/api/positions",
        json={
            "role_title": "Python Developer",
            "jd_text": "text",
            "level": "Senior",
            "must_haves": [
                "years experience",
                "Python >=3.11",
                "LLM experience",
                "RAG systems",
                "AWS proficiency",
            ],
            "nice_to_haves": ["Experience with FastAPI", "Knowledge of Docker", "Understanding of Kubernetes"],
            "tech_stack": ["Python (>=3.11,<3.12)", "AWS proficiency", "Lambda"],
            "focus_areas": [],
            "evaluation_policy": "",
            "extraction_confidence": {"overall": 0.6},
            "missing_fields": [],
        },
    )
    assert create.status_code == 200
    body = create.json()
    assert body["must_haves"] == ["Python", "LLM", "RAG", "AWS"]
    assert body["nice_to_haves"] == ["FastAPI", "Docker", "Kubernetes"]
    assert body["tech_stack"] == ["Python", "AWS", "Lambda"]


def test_manual_edit_preserves_slash_tokens(tmp_path: Path, monkeypatch) -> None:
    positions_file = tmp_path / "positions.json"

    def fake_positions_file_path() -> Path:
        return positions_file

    monkeypatch.setattr(positions_service, "_positions_file_path", fake_positions_file_path)
    app = api_server.create_app()
    client = TestClient(app)
    _login(client)

    create = client.post(
        "/api/positions",
        json={
            "role_title": "SDET",
            "jd_text": "text",
            "level": "Senior",
            "must_haves": ["Selenium"],
            "nice_to_haves": ["JUnit"],
            "tech_stack": ["Java"],
            "focus_areas": ["Testing"],
            "evaluation_policy": "",
            "extraction_confidence": {"overall": 0.8},
            "missing_fields": [],
        },
    )
    assert create.status_code == 200
    position_id = create.json()["position_id"]

    update = client.put(
        f"/api/positions/{position_id}",
        json={
            "role_title": "SDET",
            "jd_text": "text",
            "level": "Senior",
            "must_haves": ["Selenium"],
            "nice_to_haves": ["API/UI Testing", "JMeter/Locust"],
            "tech_stack": ["Java"],
            "focus_areas": ["Testing"],
            "evaluation_policy": "",
            "extraction_confidence": {"overall": 0.8},
            "missing_fields": [],
        },
    )
    assert update.status_code == 200
    body = update.json()
    assert body["nice_to_haves"] == ["API/UI Testing", "JMeter/Locust"]

    fetched = client.get(f"/api/positions/{position_id}")
    assert fetched.status_code == 200
    assert fetched.json()["nice_to_haves"] == ["API/UI Testing", "JMeter/Locust"]
