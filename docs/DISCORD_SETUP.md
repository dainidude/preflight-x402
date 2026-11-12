# Discord Sale Notifications — Setup

Get a ping in your Discord server (**ID `1512822177068482590`**) every time an
agent pays for a scan/sim. The API fires the notification automatically on
every successful settlement — you only need to give it a destination once.

## Option A — Webhook (recommended, ~1 minute, zero code)

A webhook is the standard way for a server to post into a channel. This is
what Preflight uses in production.

1. In Discord, open your server → **Server Settings → Integrations → Webhooks**
2. **New Webhook** → choose the sales channel (e.g. `#sales`) → **Copy Webhook URL**
   (URL looks like `https://discord.com/api/webhooks/151282…/A1b2…`)
3. Add it to `/home/dude/Projects/preflight-x402/.env`:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/…/…
   ```
4. Restart the API (or the launch stack), then send a test ping:
   ```
   uv run python scripts/test_discord.py
   ```
Done. Every sale posts an embed: product route, amount, payer, settlement tx.

Notes:
- Creating a webhook requires **Manage Server** permission in that server.
- The webhook URL is a secret — it's gitignored (`.env`), never commit it.
- No bot user appears in the member list; the message shows as "Preflight Sales".

## Option B — Full bot (optional, richer presence/commands)

Only needed if you want a member-list bot or slash commands later. Sale
notifications still go through the webhook (Option A); the bot adds features,
not notifications.

1. **Create the application**: https://discord.com/developers/applications →
   **New Application** → name it `Preflight Sales Bot` → create.
2. **Create the bot user**: app → *Bot* tab → **Reset Token** → copy the token
   (`DISCORD_BOT_TOKEN`). Keep it secret.
3. **Invite it to your server** — open this URL in a browser, logged into an
   account that can manage server `1512822177068482590`:
   ```
   https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID from the General tab>&scope=bot&permissions=8192
   ```
   (permissions `8192` = Manage Webhooks + Send Messages; pick the server in
   the dropdown.)
4. **Run the bot** (companion, optional — posts a ready message and can relay
   sales if you prefer one process to own all Discord traffic):
   ```
   DISCORD_BOT_TOKEN=<token> uv run --with discord.py scripts/discord_bot.py
   ```
   It logs in, creates/finds a webhook in the `#sales` channel, prints the
   webhook URL to paste into `.env` (Option A), and keeps a presence online.

## Verify end-to-end

```
# with the API running in paid mode and a funded burner:
uv run python client/pay_client.py            # spends $0.01 → ping arrives in #sales
curl -s http://127.0.0.1:8000/metrics         # revenue_base_units incremented
```

## Troubleshooting

- **No ping after a sale**: run `uv run python scripts/test_discord.py` — if it
  fails, the URL is wrong or the webhook was deleted. Server logs print
  `[preflight:notify] discord webhook failed: …` without breaking payments.
- **Sales appear but metrics don't**: settlement didn't confirm — check
  `payment_required_total` vs `settlements_total` in `/metrics`.
