#!/usr/bin/env python3
"""Send a fake sale notification to the configured Discord webhook.

Usage:  uv run python scripts/test_discord.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from preflight.notify.discord import _webhook_url, notify_sale  # noqa: E402


async def main() -> int:
    if not _webhook_url():
        print("DISCORD_WEBHOOK_URL is not set — add it to .env first (see docs/DISCORD_SETUP.md)")
        return 1
    sent = await notify_sale(
        route="POST /v1/scan (TEST — not a real sale)",
        amount_base_units=10_000,
        payer="0xc5925C971f5628ba1E195C83Ce83317A70b3f30B",
        tx_hash="0x" + "ab" * 32,
    )
    print("sent!" if sent else "send failed — check the URL and channel permissions")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
