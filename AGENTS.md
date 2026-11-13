# Preflight — Build Coordination (READ FIRST)

**Product**: pay-per-call smart-contract & token security API. Agents POST Solidity source, a transaction, or a token ADDRESS, and receive a severity-scored, EIP-712-attested verdict. Payments via **x402** (HTTP 402 + real USDC on **Base mainnet**; `base-sepolia` remains the dev sandbox).

## Ownership map — DO NOT edit outside your area

| Area | Owner | Path |
|---|---|---|
| Analysis pipeline, detectors, scoring, simulation, token-risk core | **hermes / x402-sim** | `src/preflight/core/**`, `tests/fixtures/**`, `tests/test_core*.py` |
| FastAPI app, x402 payment wiring, pricing, cache, rate limit, metrics | **omp + arbiter** | `src/preflight/api/**`, `tests/test_api*.py` |
| MCP server, discovery files, paying client demo | **crush** | `src/preflight/mcp/**`, `client/**`, `public/**`, `tests/test_mcp*.py` |
| Scaffold, types contract, config, attestation, integration, deploy | **opencode (arbiter)** | `src/preflight/types.py`, `config.py`, `attestation.py`, `pyproject.toml`, `deploy/`, `README.md` |

If you need a change in another area: **stop, and report it in your final summary** instead of editing.

## Hard rules

1. **No new dependencies** without arbiter approval. Everything you need is in `pyproject.toml`; `uv.lock` is COMMITTED — deploys are reproducible. Do NOT run `uv add`/`uv sync` (the arbiter owns the lock).
2. Run everything with **`uv run`** from the repo root. Do NOT create your own venv.
3. Python 3.12+. Type hints required on public functions. `ruff`-clean style, 100-col lines.
4. **Never commit secrets.** Keys live in `.env` (gitignored) and the private backup repo only.
5. Tests must pass offline: mark anything needing network/live-RPC with `@pytest.mark.network`.
6. When done: update `docs/STATUS_<area>.md` with what you built and how to run it. Paste the real suite pass count in your reply — the arbiter runs the suite independently and treats unverified "done" claims as failures.
7. **This is a live mainnet service with real money.** Never run anything that spends USDC without the arbiter's explicit instruction; use the `X-Preflight-Free: trial` header for paid-route e2e tests.

## The interface contract (defined in `src/preflight/types.py` — arbiter owns it)

- `core.pipeline.run_analysis(source, contract_name?, solc_version?) -> ScanReport` — SYNC, CPU-bound (solc + Slither), guarded by a 3-slot semaphore; the API calls it via `_call_core` which accepts sync or async.
- Routes & prices: `POST /v1/scan` **$0.01** · `POST /v1/sim` **$0.05** (eth_call) · `POST /v1/sim/deep` **$0.25** (anvil fork: gas, balance deltas, traces) · `POST /v1/token-risk` **$0.05** (address-in: honeypot, tax %, owner privileges — the trading-agent pivot) · `POST /v1/scan/project` **$0.10** · `POST /v1/address-risk` **$0.005** (checksum + contract/EOA + bytecode flags).
- Network: **`base` (mainnet)**, USDC `0x833589…02913`; facilitator: **CDP** (`api.cdp.coinbase.com/platform/v2/x402`) when `CDP_API_KEY_ID/SECRET` are set (production), else the x402.org public testnet facilitator. Deploying mainnet without CDP keys is refused at deploy time.
- **DEV_MODE=1** bypasses payment (`X-Preflight-Dev: bypass` header). Default OFF.
- Free tier: **opt-in** via `X-Preflight-Free: trial` — 10 free calls/day per IP+UA identity (x-payer is NOT trusted for quotas). Crawlers always see the raw 402; the 402 body's `hint` field teaches the free path.
- Rate limits: 10 req/min per payer + **$10/day per-payer cap** (approximated by counting settled spend).
- Optional enterprise key layer: `PREFLIGHT_API_KEYS` (comma-separated; default off) → paid routes require `X-Preflight-Key`.
- MCP surface: tools `preflight_check`, `preflight_sim`, `preflight_token_risk`, `preflight_pricing` — all call the HTTP API (Accept: application/json) so payments flow through x402.
- Client demo (crush): `client/pay_client.py --route scan|sim|sim-deep|token-risk|project|address-risk`.
- Attestation (arbiter): EIP-712 over `(source_sha256, verdict, findings_root)` via `PREFLIGHT_ATTEST_KEY`; every report carries it.

## Env vars

```
PREFLIGHT_PAY_TO_ADDRESS=<receiving wallet>   # REQUIRED in prod; validated at boot AND per paid request
PREFLIGHT_NETWORK=base                        # production; base-sepolia = dev sandbox
PREFLIGHT_FACILITATOR=https://x402.org/facilitator   # fallback when CDP keys unset
PREFLIGHT_PORT=8000
DEV_MODE=0
FREE_TIER=1
STATE_DIR=                                  # Modal Volume /data/state in prod
PREFLIGHT_ATTEST_KEY=<hex>
CDP_API_KEY_ID= / CDP_API_KEY_SECRET=       # CDP facilitator (production)
CLIENT_PRIVATE_KEY=                         # burner for paid smoke tests (client/ only)
PREFLIGHT_API_KEYS=                         # optional enterprise key layer (off when empty)
RPC_URL=https://mainnet.base.org
DISCORD_WEBHOOK_URL=                        # sale pings + digest + heartbeat alerts
```
