from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import api_server


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "demo", "password": "demo-pass"},
    )
    assert response.status_code == 200


def test_transcript_append_status_download(tmp_path: Path, monkeypatch) -> None:
    transcript_dir = tmp_path / "transcripts"

    def fake_transcript_file_path(room: str) -> Path:
        return transcript_dir / f"{room}.jsonl"

    monkeypatch.setattr(api_server, "_transcript_file_path", fake_transcript_file_path)
    app = api_server.create_app()
    client = TestClient(app)
    _login(client)

    status_before = client.get("/api/transcripts/demo-room/status")
    assert status_before.status_code == 200
    assert status_before.json()["exists"] is False

    append = client.post(
        "/api/transcripts/append",
        json={
            "room": "demo-room",
            "speaker": "Ankur",
            "text": "Hello team",
            "source": "livekit",
            "unique_key": "seg-1",
        },
    )
    assert append.status_code == 200
    assert append.json()["appended"] is True

    duplicate = client.post(
        "/api/transcripts/append",
        json={
            "room": "demo-room",
            "speaker": "Ankur",
            "text": "Hello team",
            "source": "livekit",
            "unique_key": "seg-1",
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["appended"] is False

    status_after = client.get("/api/transcripts/demo-room/status")
    assert status_after.status_code == 200
    assert status_after.json()["exists"] is True
    assert status_after.json()["line_count"] == 1

    download = client.get("/api/transcripts/demo-room/download")
    assert download.status_code == 200
    assert "attachment; filename=" in download.headers["content-disposition"]
    assert "Ankur (livekit): Hello team" in download.text
