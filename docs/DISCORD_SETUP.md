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

## GitHub Actions monitoring (no extra accounts)

The GitHub repository runs two scheduled workflows that post to the **same
webhook** as sale notifications. No Discord bot, uptime service, or additional
account is required — GitHub Actions is the scheduler and the Discord webhook is
the alert channel.

1. **Uptime probe (every 15 minutes).** The workflow checks the production
   `/health` endpoint and expects the paid-route 402 challenge. On failure it
   posts a Discord alert; when the service recovers it posts a recovery notice.
   This is deliberately independent of the API process, so a dead container
   still produces an alert.
2. **Monthly keepalive reminder.** If no settlement has been recorded in 25+
   days, the workflow posts a reminder to check the deployment, payer balance,
   facilitator status, and Bazaar listing. It is a nudge, not a hard failure.

Both workflows read the webhook from the **GitHub repository secret**
`DISCORD_WEBHOOK_URL` (Settings → Secrets and variables → Actions → New
repository secret). The same value is configured in the server's `.env` for
sale notifications, but GitHub Actions cannot read the repository's `.env`;
set the secret separately. Keep both copies in sync when rotating the webhook.

Workflow files live in `.github/workflows/` and are added by the arbiter; this
section documents the expected behaviour and secret location, not the workflow
YAML. If a workflow is missing or has never run, check the Actions tab and the
repository secret before changing the webhook.
