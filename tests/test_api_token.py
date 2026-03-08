from fastapi.testclient import TestClient

from app.api_server import create_app


def test_login_and_me() -> None:
    app = create_app()
    client = TestClient(app)

    login = client.post(
        "/api/auth/login",
        json={"username": "demo", "password": "demo-pass"},
    )
    assert login.status_code == 200

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "demo"


def test_token_creation_without_ai_dispatch() -> None:
    app = create_app()
    client = TestClient(app)

    login = client.post(
        "/api/auth/login",
        json={"username": "demo", "password": "demo-pass"},
    )
    assert login.status_code == 200

    token_resp = client.post(
        "/api/token",
        json={
            "room": "demo-room",
            "ai_enabled": False,
            "capabilities": {
                "can_publish": True,
                "can_subscribe": True,
                "can_publish_data": True,
                "can_publish_sources": ["microphone", "camera"],
            },
        },
    )
    assert token_resp.status_code == 200
    body = token_resp.json()
    assert body["room"] == "demo-room"
    assert body["token"]
    assert body["server_url"]
