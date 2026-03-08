from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.agent_dispatch import ensure_agent_for_room
from shared.config import settings

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def reconcile_room(room: str, agent: str, instructions: str | None) -> None:
    result = await ensure_agent_for_room(room=room, agent=agent, instructions=instructions)
    logging.info(
        "reconcile room=%s exists=%s has_humans=%s has_agent=%s had_dispatch=%s created_dispatch=%s",
        room,
        result.room_exists,
        result.has_humans,
        result.has_agent,
        result.had_valid_dispatch,
        result.created_dispatch,
    )


async def run_loop(room: str, agent: str, interval: float, instructions: str | None) -> None:
    while True:
        try:
            await reconcile_room(room=room, agent=agent, instructions=instructions)
        except Exception as exc:  # noqa: BLE001
            logging.warning("room reconcile failed: %s", exc)
        await asyncio.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Keep room agent lifecycle aligned with human presence")
    parser.add_argument("--room", default="demo-room")
    parser.add_argument("--agent", default=settings.default_agent)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--instructions", default=None)
    args = parser.parse_args()

    asyncio.run(
        run_loop(
            room=args.room,
            agent=args.agent,
            interval=args.interval,
            instructions=args.instructions,
        )
    )


if __name__ == "__main__":
    main()
