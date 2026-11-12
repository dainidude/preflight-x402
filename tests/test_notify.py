"""Offline tests for the Discord sale notifier (no network)."""

from __future__ import annotations

import asyncio

import pytest

from preflight.notify import discord as dn


def test_notify_noop_without_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    sent = asyncio.run(dn.notify_sale("POST /v1/scan", 10_000, "0xabc", "0x" + "11" * 32))
    assert sent is False


def test_notify_sends_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_send(url: str, payload: dict) -> None:
        calls.append((url, payload))

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
    monkeypatch.setattr(dn, "_send", fake_send)
    sent = asyncio.run(dn.notify_sale("POST /v1/sim", 50_000, "0xabc", "0x" + "22" * 32))
    assert sent is True
    url, payload = calls[0]
    assert "webhooks/test" in url
    embed = payload["embeds"][0]
    assert embed["title"] == "💸 New sale"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Amount"] == "$0.050"
    assert "POST /v1/sim" in fields["Product"]


def test_notify_failure_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(url: str, payload: dict) -> None:
        raise RuntimeError("webhook down")

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
    monkeypatch.setattr(dn, "_send", boom)
    sent = asyncio.run(dn.notify_sale("POST /v1/scan", 10_000, None, "0xdead"))
    assert sent is False  # never raises — payments unaffected
