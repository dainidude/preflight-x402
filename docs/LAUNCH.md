# Preflight — Launch Checklist & Runbook

Status: **testnet launch ready** (Base Sepolia). Mainnet is a config flip away — see below.

## 1. Service routes

| Route | Price | Purpose |
|---|---|---|
| `GET /` | free | service info + price list |
| `GET /health` | free | liveness |
| `GET /metrics` | free | funnel/revenue instrumentation |
| `GET /llms.txt` | free | LLM-facing service description |
| `GET /.well-known/x402.json` | free | x402/Bazaar discovery metadata |
| `POST /v1/scan` | $0.01 | single-file Solidity security scan |
| `POST /v1/sim` | $0.05 | transaction pre-flight simulation (RPC `eth_call`, free to run — pure margin) |
| `POST /v1/scan/project` | $0.10 | multi-file project scan |
| `POST /v1/address-risk` | $0.005 | address risk heuristics |

Free tier: first 10 scans/day per client fingerprint (IP+UA hash or payer address) when `FREE_TIER=1`.
Rate limit: 10 req/min per payer. Cache: source-hash LRU (512 entries, 1h TTL).

## 2. Environment

All in `.env` (gitignored; copy `.env.example`):

```
PREFLIGHT_PAY_TO_ADDRESS=<receiving wallet>   # REQUIRED in prod mode
PREFLIGHT_NETWORK=base-sepolia               # or "base" for mainnet
PREFLIGHT_FACILITATOR=https://x402.org/facilitator
PREFLIGHT_PORT=8000
DEV_MODE=0                                   # 1 = payment bypass (dev only!)
FREE_TIER=1
PREFLIGHT_ATTEST_KEY=<hex key>               # EIP-712 report attestations
RPC_URL=https://sepolia.base.org             # or a Base mainnet RPC
```

## 3. Start the stack

```bash
uv run uvicorn preflight.api.app:app --host 0.0.0.0 --port 8000 &
./scripts/launch_tunnel.sh          # public trycloudflare.com URL (ephemeral)
uv run python client/pay_client.py  # smoke: real 402 → pay → verdict
```

Full testnet demo: `./scripts/run_demo.sh testnet` · offline suite: `./scripts/run_demo.sh tests`.

## 4. Go public (directory listing)

With a **permanent** URL live (x402scan rejects ephemeral tunnel URLs):

1. Verify from outside: `curl -i <PERMANENT_URL>/v1/scan -d '{"source":"contract A{}"}' -H 'Content-Type: application/json'` → must be `402` with `PAYMENT-REQUIRED`.
2. Submit to the directories:
   - **x402scan.com/resources/register** — paste the permanent URL (30 s)
   - **Bazaar** — server extension already registered (`x402.extensions.bazaar`); CDP indexes by `payTo`
   - **agentic.market**, **pay.sh**, **x402-list.com**
   - **awesome-agentic-commerce** — PR #883 open
3. Post the `llms.txt` + MCP server (`uv run python -m preflight.mcp.server`) to agent-dev Discords/communities.

Quick-tunnel URLs are **ephemeral** (die with the process). Permanent options:

- **Modal (recommended, in use)**: `set -a && source .env && set +a && modal deploy deploy/modal_app.py`
  → permanent `https://<workspace>--preflight-api--8000.modal.run`, always-on via `min_containers=1`,
  solc pre-baked in the image, secrets injected from local env. ~$5-10/mo within the $30 free credits.
- **Cloudflare named tunnel**: requires a **domain added to your Cloudflare account** (`cloudflared
  tunnel login` shows zones to pick — if the picker is empty, your account has no domains yet).
  Free once you own a zone.
- **Fly.io/Modal alternatives**: container with `uvicorn preflight.api.app:app`, env vars as above.

## 5. Mainnet flip (when ready to earn real USDC)

1. Fund/choose a **real receiving wallet**; set `PREFLIGHT_PAY_TO_ADDRESS`.
2. Set `PREFLIGHT_NETWORK=base` (USDC asset auto-switches to `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`), `RPC_URL` to a Base RPC.
3. Facilitator: keep `https://x402.org/facilitator` (supports mainnet) or switch to Coinbase CDP for KYT/OFAC screening per call.
4. Update `public/.well-known/x402.json` values (network `eip155:8453`, asset, resource URL).
5. Re-run the paid smoke test with a small amount of real USDC.

## 6. Operating notes

- **Attestations**: every report carries an EIP-712 attestation over `(source hash, verdict, findings root)` — buyers can prove what Preflight said. Keep `PREFLIGHT_ATTEST_KEY` backed up and stable; rotating it invalidates old attestation verification trust.
- **Liability**: `looks_ok` ≠ audit. `llms.txt` says so; keep the disclaimer in all published copy.
- **Funnel**: watch `GET /metrics` — `payment_required_total` (interest) vs `settlements_total` (conversion) vs `scans_by_verdict` (product quality). Revenue is in `revenue_base_units` (USDC 6-decimals).
- **Abuse**: free tier is per-fingerprint in-memory (per-process); a public deployment should move `UsageTracker` to Redis (2h work, see AGENTS.md pattern).

## 7. Post-launch roadmap (from the product plan)

- Tune detector false positives against real DeFi contracts weekly (fixtures exist for regression).
- Add `batch-settlement` scheme for high-frequency polling agents (x402 package supports it).
- Cross-sell "Lens" (decoded on-chain events) to the same crypto-native buyers — plan note in dud3-vault.
- Target: ~50 paying agents by day 60–90 per the plan's honest-expectation section.
