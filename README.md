# LiveKit Multi-Agent Voice + Meet-Style Video App (Python + React)

This project now includes:

- LiveKit Agents worker (Python) for AI voice participants.
- FastAPI backend for secure token issuance and session auth.
- React web meeting app using LiveKit Components for video calls.

## Architecture

- `app/main.py`: LiveKit agent worker.
- `app/api_server.py`: Auth + token API (`/api/auth/*`, `/api/token`, `/api/metrics`).
- `shared/agent_dispatch.py`: Shared room/dispatch logic to auto-invite AI participant once humans join.
- `frontend/`: Vite + React conferencing UI.

## Setup

1. Install Python deps:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Configure environment:

```bash
cp .env.example .env
```

Required values:

- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`
- `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`
- Optional realtime tuning: `OPENAI_REALTIME_MODEL`, `OPENAI_REALTIME_VOICE`
- `EXPLICIT_DISPATCH=true` (recommended, prevents auto-dispatch for every room)
- `APP_AUTH_USER`, `APP_AUTH_PASSWORD`, `APP_SESSION_SECRET`

3. Install frontend deps:

```bash
cd frontend
cp .env.example .env
npm install
```

## Run locally

Run each process in its own terminal.

1. Start LiveKit server (self-hosted) separately.
2. Start AI agent worker:

```bash
source .venv/bin/activate
python app/main.py --agent assistant start
```

3. Start API server:

```bash
source .venv/bin/activate
python app/api_server.py
```

4. Start React app:

```bash
cd frontend
npm run dev
```

Open `http://localhost:5173`.

## API contract

### `POST /api/auth/login`

Input:

```json
{ "username": "demo", "password": "demo-pass" }
```

Output sets secure HTTP-only session cookie and returns:

```json
{ "username": "demo" }
```

### `POST /api/token`

Input:

```json
{
  "room": "demo-room",
  "display_name": "Ankur",
  "ai_enabled": true,
  "agent": "assistant",
  "instructions": "Answer briefly",
  "capabilities": {
    "can_publish": true,
    "can_subscribe": true,
    "can_publish_data": true,
    "can_publish_sources": ["microphone", "camera", "screen_share"]
  }
}
```

Output:

```json
{
  "token": "<jwt>",
  "server_url": "ws://localhost:7880",
  "identity": "demo-a1b2c3d4",
  "room": "demo-room",
  "expires_at": "2026-03-08T12:00:00+00:00"
}
```

## Room metadata/dispatch contract

For AI-enabled rooms, dispatch metadata is JSON with:

- `agent`: logical agent name (`assistant` / `support` / `interviewer` / `realtime` / `observer`)
- `room_mode`: `human_ai`
- `instructions`: optional free-form instructions

### `POST /api/openai/realtime/token`

Creates a short-lived OpenAI Realtime client secret for browser WebRTC sessions.

Input:

```json
{
  "model": "gpt-realtime-mini",
  "voice": "alloy",
  "instructions": "Be concise"
}
```

Output:

```json
{
  "client_secret": "<ephemeral-secret>",
  "model": "gpt-realtime-mini",
  "voice": "alloy"
}
```

## Tests

```bash
source .venv/bin/activate
pytest -q
```

## Notes for production

- Set `COOKIE_SECURE=true` behind HTTPS.
- Restrict `API_CORS_ORIGINS` to your frontend domains.
- Set `METRICS_BEARER_TOKEN` to protect `/api/metrics`.
- Ensure TURN/TLS is configured for restrictive networks.
