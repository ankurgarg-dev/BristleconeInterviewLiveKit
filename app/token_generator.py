from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from livekit.api import AccessToken, VideoGrants

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.config import settings

load_dotenv()


def generate_token(room: str, identity: str, name: str | None = None) -> str:
    token = (
        AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(name or identity)
        .with_grants(
            VideoGrants(
                room_join=True,
                room=room,
                can_subscribe=True,
                can_publish=True,
                can_publish_data=True,
            )
        )
    )
    return token.to_jwt()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a local LiveKit access token")
    parser.add_argument("--room", required=True, help="Room name")
    parser.add_argument("--identity", required=True, help="Participant identity")
    parser.add_argument("--name", default=None, help="Display name")
    args = parser.parse_args()

    print(generate_token(room=args.room, identity=args.identity, name=args.name))


if __name__ == "__main__":
    main()
