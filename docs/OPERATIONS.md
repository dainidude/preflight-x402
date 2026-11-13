# Preflight Operations Runbook

Answers the standing questions: what runs where, what it costs, what to do and never do.

## What runs where — the PC question

**The whole service runs in Modal's cloud. Your PC can be off for weeks.**

| Component | Where it lives | Needs your PC? |
|---|---|---|
| The API (all 5 paid routes) | Modal container, always-on (`min_containers=1`) | No |
| Metrics/quotas (durable) | Modal Volume `preflight-state` | No |
| Daily Discord digest | Modal Cron, 09:00 UTC | No |
| Heartbeat probe | Modal Cron, every 5 min | No |
| Payments (verify/settle) | Coinbase CDP facilitator | No |
| Bazaar/x402scan listings | Coinbase + x402scan's crawlers | No |

Your PC is only needed for: **deploying changes** (`modal deploy`), running local scripts (`check_bazaar.py`, paid smoke tests), and the repo. None of those affect the running service.

## Costs (verified Nov 2026, modal.com/pricing)

Plan: **Starter — $0/month platform fee, $30/month free compute credits**. A card on file is required by Modal's billing terms; usage beyond the $30 credits would be auto-charged.

Preflight's burn rate:

| Item | Math | ~Cost/month |
|---|---|---|
| Always-on container (idle floor: 0.125 core + ~128 MiB) | 0.125 × $0.0000131/s × 2.6M s + 0.125 GiB × $0.00000222/s × 2.6M s | **~$5** |
| Request bursts (scans: seconds of CPU each) | negligible at current volume | ~$0–1 |
| Crons (digest + heartbeat) | ~5 min of compute spread over the month | ~$0.01 |
| Volume (metrics, < 1 GiB; 1 TiB free) | — | $0 |
| Egress (KB responses; 1 TiB free) | — | $0 |

**Total: ~$5-6/month, fully inside the $30 free credits → $0 out of pocket.** Scaling note: each additional always-on container or big traffic spike adds linearly; a 10× traffic increase still stays roughly free (bursts are pennies).

Check actual spend at modal.com → Settings → Usage & Billing. `modal billing` CLI can export reports.

## DO — keeping it healthy

- **Back up the repo's `.env` somewhere safe OFF this machine** (password manager). It holds the only copies of: `CDP_API_KEY_ID/SECRET`, `PREFLIGHT_ATTEST_KEY`, `CLIENT_PRIVATE_KEY` (burner), `DISCORD_WEBHOOK_URL`. The GitHub repo + those values = everything needed to rebuild from scratch on any machine: clone, write .env, `uv sync`, `modal deploy`.
- **Watch the Discord channel** — it is your ops dashboard: sale pings, the 09:00 UTC daily digest, and heartbeat outage alerts all land there.
- **Keep one paid call per month**: the CDP Bazaar drops resources with no settlement for 30 days. If real customers haven't paid in a month, run `scripts/index_bazaar.py` ($0.01) to keep the listing alive.
- **Occasionally verify the full stack**: `scripts/run_demo.sh prod` (free — checks health, the 402 challenge, and metrics).
- **Keep the CDP API key valid**; if you rotate it, update `.env` and redeploy.
- **If you change files without touching the image**: `modal app stop preflight-api -y` then `modal deploy ...` — mounted files only refresh at container start.
- **Set a Modal spend budget** (Settings → Budgets) if you want a hard ceiling.

## DON'T — the traps

- **Don't delete the Volume** (`preflight-state`): it's the only durable copy of metrics/quotas. Deleting it zeroes revenue history.
- **Don't rotate `PREFLIGHT_ATTEST_KEY` casually**: every old attestation verifies against that key's signer. Rotating invalidates the trust for past reports.
- **Don't commit or paste `.env` values anywhere** (chat included). If a key leaks: rotate it — CDP keys are regenerated from the portal, the webhook is recreated in Discord, the burner key means moving funds to a fresh burner.
- **Don't leave `modal app stop` without a deploy** — that's downtime. Stop only immediately before redeploying.
- **Don't run paid flows with real money casually**: use the `X-Preflight-Free: trial` header for e2e tests; the burner only holds a few dollars.
- **Don't assume silence means health**: the heartbeat itself runs on Modal — if the whole platform (not just the app) is down, the alerter is down with it. For full external cover, add a free uptime monitor (UptimeRobot/Better Stack) pinging `/health` — optional, recommended once there's real revenue.

## Failure modes and recovery

| Failure | Symptom | Recovery |
|---|---|---|
| App bug after deploy | Heartbeat alert ×2 in Discord | `git revert` in the repo, redeploy |
| Modal workspace outage | Everything dark; no alerts (alermen are on Modal too) | Check status.modal.com; nothing to do locally |
| Facilitator settles failing | `X-Preflight-Settle-Error` headers / dead-letter file grows | Retries already handle most; `scripts/settle_repair.py` replays the rest |
| CDP key revoked | 401s on every paid request, free routes still fine | New key from portal.cdp.coinbase.com → `.env` → redeploy |
| Lost laptop | PC gone; service unaffected | Restore repo + `.env` backup on any machine, redeploy, update local tooling |
| Zero customers for 30 days | Bazaar listing dropped | Run one paid call; listing re-indexes |

## Rebuild from scratch (disaster recovery)

```bash
git clone https://github.com/dainidude/preflight-x402 && cd preflight-x402
# restore .env from backup (or re-create: see .env.example + portal.cdp.coinbase.com)
uv sync --extra dev
uv run pytest tests/ -m "not network"     # verify the tree
set -a && source .env && set +a
uv tool install modal                       # if fresh machine
modal token new                             # re-auth the workspace
modal deploy deploy/modal_app.py            # Volume auto-recreates; metrics start fresh
```

The x402scan listing (https://www.x402scan.com/server/1961af3d-afed-400b-bc57-ca4ebe41ef18) survives rebuilds — it points at the URL, not the container. The Bazaar re-indexes after the first settled payment post-rebuild.
