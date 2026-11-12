# Preflight — x402-paid smart-contract pre-flight security API

Pay-per-call security scans and transaction simulations for AI agents.
POST Solidity source or a transaction, receive a severity-scored,
**EIP-712-attested** verdict: `looks_ok | review | block`.
Payments over the **x402 protocol** (HTTP 402 + USDC on Base Sepolia testnet).

Built from the product plan in `dud3-vault/LLM/2026-11-08 - x402 Product Plans.md`
by hermes + omp + crush + opencode in a 4-pane Herdr session.

## Try it

```bash
# no wallet needed (server DEV_MODE=1):
uv run uvicorn preflight.api.app:app --port 8000 &
uv run python client/pay_client.py --dev

# real x402 payment (funded burner, $0.01):
uv run python client/pay_client.py

# the handshake, by hand:
curl -i -X POST http://127.0.0.1:8000/v1/scan -H 'Content-Type: application/json' \
  -d '{"source":"contract A {}"}'
# → 402, PAYMENT-REQUIRED header (base64 JSON requirements)
# → retry with PAYMENT-SIGNATURE header → verdict + PAYMENT-RESPONSE
```

## Endpoints & prices

| Route | Price |
|---|---|
| `POST /v1/scan` — single-file security scan (solc+Slither + 5 custom DeFi detectors) | **$0.01** |
| `POST /v1/sim` — transaction pre-flight simulation (RPC `eth_call`, revert decode) | **$0.05** |
| `POST /v1/scan/project` — multi-file project scan | **$0.10** |
| `POST /v1/address-risk` — address risk heuristics | **$0.005** |
| `GET /` `GET /health` `GET /metrics` `GET /llms.txt` `GET /.well-known/x402.json` | free |

First 10 scans/day per client are free (funnel). 10 req/min per payer.

## Architecture

```
client/pay_client.py        paying demo agent (402 → x402.client pay → retry)
src/preflight/
  core/    pipeline (solc+Slither), custom detectors, scoring,
           simulation (RPC eth_call + revert decoding)
  api/     FastAPI app, real x402 middleware (x402 pkg, x402.org facilitator),
           cache (source-hash LRU), ratelimit, metrics
  mcp/     MCP server: preflight_check, preflight_sim, preflight_pricing
public/    llms.txt + .well-known/x402.json (discovery)
```

- **Payments**: `x402` package v2 — `exact` scheme, USDC, Base Sepolia;
  facilitator verifies + settles; server never holds custody.
- **Attestations**: EIP-712 over `(source_sha256, verdict, findings_root)` — buyers can prove what we said.
- **Determinism**: static analysis + `eth_call` simulation; verdicts are reproducible.

## Docs

- `AGENTS.md` — ownership map & interface contract
- `docs/LAUNCH.md` — launch checklist, tunnel, mainnet flip, operating notes
- `docs/STATUS_*.md` — per-area build reports

## Test

```bash
uv run pytest tests/ -m "not network"   # offline suite
uv run pytest tests/ -m network          # live-RPC + real paid flows (spends test USDC)
```
