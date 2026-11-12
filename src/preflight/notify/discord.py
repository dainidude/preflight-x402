"""Discord sale notifications for Preflight.

Posts a webhook message on every successful settlement. Fire-and-forget:
notification failures are logged but NEVER affect the payment flow.

Setup (see docs/DISCORD_SETUP.md):
  1. Discord → Server Settings → Integrations → Webhooks → New Webhook → Copy URL
  2. Put it in .env as DISCORD_WEBHOOK_URL=...
  3. Restart the API. Every sale pings your channel.

Test without a sale:  uv run python scripts/test_discord.py
"""

from __future__ import annotations

import os
from typing import Any

import httpx

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"


def _webhook_url() -> str | None:
    return os.getenv(WEBHOOK_ENV) or None


def _embed(route: str, amount_base_units: int, payer: str | None, tx_hash: str) -> dict[str, Any]:
    usdc = amount_base_units / 1_000_000
    color = 0x00C853 if usdc >= 0.05 else 0x2979FF
    fields = [
        {"name": "Product", "value": f"`{route}`", "inline": True},
        {"name": "Amount", "value": f"${usdc:.3f}", "inline": True},
        {"name": "Payer", "value": f"`{payer or 'unknown'}`", "inline": False},
        {"name": "Settlement tx", "value": f"`{(tx_hash or "unknown")[:20]}`", "inline": False},
    ]
    return {
        "username": "Preflight Sales",
        "embeds": [
            {
                "title": "💸 New sale",
                "color": color,
                "fields": fields,
                "footer": {"text": "Preflight — x402 smart-contract security"},
            }
        ]
    }


async def _send(url: str, payload: dict[str, Any]) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()


async def notify_sale(route: str, amount_base_units: int, payer: str | None, tx_hash: str) -> bool:
    """Post a sale notification. Returns True when sent.

    No-op (returns False) when DISCORD_WEBHOOK_URL is unset; never raises.
    """
    url = _webhook_url()
    if not url:
        return False
    try:
        await _send(url, _embed(route, amount_base_units, payer, tx_hash))
        return True
    except Exception as exc:  # notifications must never break payments
        print(f"[preflight:notify] discord webhook failed: {exc}")
        return False
