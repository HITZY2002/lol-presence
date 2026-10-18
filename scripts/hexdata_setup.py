"""Manually sign in to the dedicated profile, optionally test one UI query."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.hexdata_browser import (  # noqa: E402
    HexdataBrowser,
    HexdataError,
)


async def main() -> None:
    """Wait for human login without automating authentication or exporting it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument(
        "--channel", choices=["msedge", "chromium", "chrome"], default="msedge"
    )
    parser.add_argument("--game-id", default="")
    args = parser.parse_args()
    service = HexdataBrowser(
        args.data_dir, {"enabled": True, "browser_channel": args.channel}
    )
    try:
        await service.open_login()
        print("Dedicated Hexdata browser opened. Complete login manually.", flush=True)
        await asyncio.to_thread(input, "Press Enter after completing login: ")
        if args.game_id:
            card = await service.get_card(args.game_id)
            print(
                json.dumps(
                    {"card": str(card.path), "game_id": card.game_id},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    except HexdataError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
