#!/usr/bin/env python3
"""Optional companion bot for Preflight sale notifications (Option B).

NOT required for notifications — the webhook (Option A in
docs/DISCORD_SETUP.md) is the production path. This bot:

  * logs in and shows a presence ("watching for sales"),
  * on startup, finds or creates a webhook named "Preflight Sales" in the
    DISCORD_SALES_CHANNEL (default: first text channel),
  * prints that webhook URL so you can paste it into .env.

Run:  DISCORD_BOT_TOKEN=<token> uv run --with discord.py scripts/discord_bot.py
"""

from __future__ import annotations

import asyncio
import os
import sys

GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "1512822177068482590"))
SALES_CHANNEL = os.getenv("DISCORD_SALES_CHANNEL")  # optional channel name
WEBHOOK_NAME = "Preflight Sales"


async def main() -> int:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("DISCORD_BOT_TOKEN not set — see docs/DISCORD_SETUP.md Option B")
        return 1

    import discord  # provided via: uv run --with discord.py

    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready() -> None:
        guild = discord.utils.get(bot.guilds, id=GUILD_ID) or (
            bot.guilds[0] if bot.guilds else None
        )
        if guild is None:
            print("Bot is not in the target server — re-invite it with the OAuth2 link.")
            await bot.close()
            return
        channel = (
            discord.utils.get(guild.text_channels, name=SALES_CHANNEL)
            if SALES_CHANNEL
            else None
        ) or guild.text_channels[0] if guild.text_channels else None
        if channel is None:
            print("No text channel found in the server.")
            await bot.close()
            return

        webhook = discord.utils.get(await channel.webhooks(), name=WEBHOOK_NAME)
        if webhook is None:
            webhook = await channel.create_webhook(name=WEBHOOK_NAME)
        print(f"Bot ready in '{guild.name}' — sales channel: #{channel.name}")
        print(f"\nPaste this into .env as DISCORD_WEBHOOK_URL:\n{webhook.url}\n")
        # Stay online for presence; Ctrl+C to stop.
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="for sales"))

    try:
        await bot.start(token)
    except discord.LoginFailure:
        print("Invalid DISCORD_BOT_TOKEN.")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
