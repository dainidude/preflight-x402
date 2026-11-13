# Preflight MCP / Client / Discovery — Status

Owner: crush. Area: `src/preflight/mcp/**`, `client/**`, `public/**`,
`tests/test_mcp*.py`, `tests/test_pay_client.py`.

## What was built

- `src/preflight/mcp/server.py` — MCP server on the official `mcp` SDK
  (`MCPServer`, mcp 2.x). Tools:
  - `preflight_check {source, contract_name?}` → POSTs `/v1/scan` and returns
    the `ScanReport` JSON (validated through `ScanReport.model_validate`).
  - `preflight_pricing` → free `GET /`.
  - stdio by default (`uv run python -m preflight.mcp.server`), plus
    `--http/--host/--port/--path` for streamable HTTP. When `DEV_MODE=1` it
    sends `X-Preflight-Dev: bypass`; a 402 surfaces as a `ToolError`.
- `client/pay_client.py` — paying-agent demo. Loads/generates a burner key in
  `.env` (`CLIENT_PRIVATE_KEY`), checks Base Sepolia test-USDC balance via RPC,
  and walks the full x402 flow. `--dev` skips the wallet (server DEV_MODE).
  `--file`/`--source`/`--contract-name`/`--url` override the defaults. Inline
  reentrancy sample is self-contained.
- `public/llms.txt` — LLM-facing service description with endpoints, prices,
  request/response shapes, and curl examples.
- `public/.well-known/x402.json` — x402 discovery metadata (resource, pricing
  per route, network/asset/scheme, `pay_to` placeholder to be templated from
  `PREFLIGHT_PAY_TO_ADDRESS`).
- `tests/test_mcp_tools.py`, `tests/test_pay_client.py` — offline tests
  (mocked API / `httpx.MockTransport`); real-network cases marked
  `@pytest.mark.network`.

## How to run

```bash
# API (dev bypass) — other area, shown for context
DEV_MODE=1 PREFLIGHT_PAY_TO_ADDRESS=0x... uv run uvicorn preflight.api.app:app --port 8000

# MCP server (stdio)
uv run python -m preflight.mcp.server
# MCP server (streamable HTTP)
uv run python -m preflight.mcp.server --http --port 8000

# Paying demo client
uv run python client/pay_client.py --dev          # no wallet
uv run python client/pay_client.py                # real 402 (needs test USDC)

# Tests (offline)
uv run pytest tests/test_mcp_tools.py tests/test_pay_client.py -q -m "not network"
```

## 402 client flow (implementation)

1. `scan_paid` POSTs `/v1/scan` with no payment header.
2. Server answers 402 with a `PAYMENT-REQUIRED` header (base64 JSON — the API
   sends a bare array of accepts; the client also accepts a full
   `PaymentRequired` object or a V1 body) plus a V1-shaped body.
3. `parse_payment_required` coerces the `accepts` items through
   `x402.schemas.PaymentRequirements` and wraps them in a V2 `PaymentRequired`.
4. `build_x402_client` registers `ExactEvmScheme` (EIP-3009) for the burner
   key; `create_payment` signs the authorization via `x402.client`.
5. Retry the same POST with `PAYMENT-SIGNATURE` (base64 payment payload).
6. Server verifies via the facilitator, serves the scan, settles, and returns
   `PAYMENT-RESPONSE` (base64 `SettleResponse`).
7. Client prints verdict, findings, attestation, and the payment tx details.

## Test results

- `tests/test_mcp_tools.py` + `tests/test_pay_client.py`: **14 passed**
  (1 network test deselected by default).
- Full-suite offline run shows failures in `test_api_*`, `test_core_pipeline`,
  and `test_e2e` — all outside this area (see cross-area requests).

## Cross-area requests / blockers

1. **Core/API async mismatch (blocks a live dev scan).** Hermes made
   `run_analysis` async, but `src/preflight/api/app.py:_run_sync` calls it
   synchronously and `_finalize` does `report.model_dump()` on the coroutine →
   `POST /v1/scan` returns 500 even in DEV_MODE. Fix belongs in
   `src/preflight/api/**` (arbiter/omp).
2. **Payment facilitator is network-bound.** Paid routes need
   `PREFLIGHT_PAY_TO_ADDRESS` and a reachable facilitator; the offline
   `test_api_*`/`test_e2e` failures (`Server not initialized`, facilitator 404)
   come from that. Not this area.
3. `public/.well-known/x402.json` uses a placeholder `pay_to`
   (`0x0000…`); the API serves the file verbatim, so templating it from
   `PREFLIGHT_PAY_TO_ADDRESS` at serve time is a small arbiter-side follow-up.

## Round 2 — launch surface (discovery, multi-route client, MCP sim, paid-flow gate)

### What changed

- `public/.well-known/x402.json` — real launch values: `pay_to`
  `0x3779bb827AB1F32A317fD30157Ac03Dc5CCA88aa`, `eip155:84532` / `base-sepolia`,
  test USDC `0x036CbD53842c5426634e7929541eC2318f3dCF7e`, scheme `exact`,
  facilitator `https://x402.org/facilitator`. Prices + base-unit `amount` for
  `/v1/scan` ($0.01 / 10000), `/v1/sim` ($0.05 / 50000),
  `/v1/scan/project` ($0.10 / 100000), `/v1/address-risk` ($0.005 / 5000).
  Adds `GET /metrics`, and Bazaar-shaped `extensions.bazaar` discovery blocks
  (`info` + JSON `schema`, `method: POST`, `bodyType: json`) for every HTTP
  route plus `preflight_check` / `preflight_sim` MCP tool discovery.
- `public/llms.txt` — documents `/v1/sim` (transaction pre-flight simulation),
  `/metrics`, the `SimRequest` / `SimReport` shapes, and the full
  `402 → PAYMENT-SIGNATURE retry → verdict + PAYMENT-RESPONSE` curl loop.
- `client/pay_client.py` — multi-route: `--route scan|sim|project|address-risk`
  (default `scan`). New `--to/--data/--value-wei/--from-address` for `sim`,
  `--address` for `address-risk`, repeatable `--file` for `project`. Zero-arg
  `--route sim` runs the demo: a 0-value ERC-20 `transfer` of an unaffordable
  amount to test-USDC → `sim:revert` is printed as a correct finding, exit 0.
  Existing `--dev`, `--url`, `--source`, `--file`, `--contract-name` and the
  `scan_paid` / `scan_dev` helpers are unchanged; `post_paid` / `post_dev` /
  `paid_exchange` generalize the flow.
- `src/preflight/mcp/server.py` — new `preflight_sim {to, data?, value_wei?}`
  tool → POST `/v1/sim`, validates `SimReport`, dev bypass headers, and a
  402 `ToolError` naming the $0.05 price and `--route sim`.
- `tests/test_paid_flow.py` — network-marked launch gate: real $0.01 paid scan
  via the facilitator (asserts 200, `PAYMENT-RESPONSE`, valid `ScanReport`),
  then `GET /metrics` with `revenue_base_units >= 10000` (accepts the flat or
  `{"metrics": {...}}` shape). Skips on missing key, zero balance, DEV_MODE,
  or a free-tier grant.
- `tests/test_mcp_tools.py` / `tests/test_pay_client.py` — offline coverage for
  the sim tool, discovery values/Bazaar blocks, llms.txt surface, route table,
  ERC-20 calldata, per-route payloads and mocked paid calls.

### How to run

```bash
uv run pytest tests/test_mcp_tools.py tests/test_pay_client.py -q -m "not network"
uv run python client/pay_client.py --route sim                 # revert demo
uv run python client/pay_client.py --route sim --to 0x... --data 0x...
uv run python client/pay_client.py --route project --file A.sol --file B.sol
uv run python client/pay_client.py --route address-risk --address 0x...
uv run pytest tests/test_paid_flow.py -q -m network            # spends $0.01
```

### Test results

- Full offline suite: **76 passed, 3 deselected**
  (`uv run pytest tests/ -m "not network"`), including **19 new offline
  tests** in `tests/test_mcp_tools.py` / `tests/test_pay_client.py` on top of
  the 49-test baseline.
- `tests/test_paid_flow.py` is network-marked and deselected by default.

### Cross-area requests / blockers

1. **`GET /metrics` response shape**: the collector nests counters under
   `{"status": "ok", "metrics": {...}}`; the paid-flow test tolerates both
   nested and flat. If the launch contract is flat, no change needed.
2. **`/v1/sim` route wiring**: `payments.ROUTE_PRICES` now prices it at
   $0.05; `app.py` still needs the `POST /v1/sim` handler and the `/metrics`
   router mounted for the paid-flow gate to pass end-to-end.
3. For the paid-flow gate the API must run with `DEV_MODE=0` and
   `FREE_TIER=0`; otherwise the test skips because no settlement occurs.

## v2 surface (deep sim, mainnet polish, batch design)

### What changed

- `src/preflight/mcp/server.py` — `preflight_sim` gains `mode: "quick"|"deep"`
  (default `quick`). `deep` routes to `POST /v1/sim/deep` ($0.25); 402
  `ToolError`s name the correct price and demo route (`--route sim-deep`).
  `simulate_transaction()` validates mode and returns the full `SimReport`.
  Tool descriptions now state both prices and Base mainnet.
- `client/pay_client.py` — `--route sim-deep` (and `--route sim --deep`) posts
  `/v1/sim/deep`; sim output prints `gas_used`, `balance_delta_wei`, and
  `internal_calls`. Mainnet reality: USDC
  `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, RPC
  `https://mainnet.base.org`, and zero balance prints the Base mainnet funding
  link instead of faucet text. `--dev` behavior unchanged.
- `public/llms.txt` — `/v1/sim/deep` row ($0.25, anvil fork), quick vs deep
  description, deep `SimReport` fields, curl example, MCP `mode` docs; all
  testnet asset/network references removed.
- `public/.well-known/x402.json` — `POST /v1/sim/deep` pricing
  (`$0.25` / `250000` / `eip155:8453` / mainnet USDC), endpoint entry, Bazaar
  discovery block with anvil output example, and `mode` input on the
  `preflight_sim` MCP discovery. All testnet references removed.
- `tests/test_mcp_tools.py` — deep routing test (asserts `/v1/sim/deep`),
  deep 402 pricing, invalid-mode rejection, sim/deep discovery assertions, and
  a public-files no-testnet sweep test.
- `tests/test_pay_client.py` — `sim-deep` route/payload shape test, deep route
  in the mocked paid-flow matrix, mainnet constants/RPC.
- `docs/BATCH_SETTLEMENT.md` — one-page design (no implementation).

### How to run

```bash
uv run pytest tests/test_mcp_tools.py tests/test_pay_client.py -q -m "not network"
uv run python client/pay_client.py --route sim-deep
uv run python client/pay_client.py --route sim --deep
```

### Test results

- Full offline suite: **104 passed, 4 deselected**
  (`uv run pytest tests/ -m "not network"`).
- Crush files: **39 passed, 1 deselected**.

### Remaining testnet references

- None in `public/`, `client/`, or `src/preflight/mcp/`. The only remaining
  literals are the intentional guard list inside
  `test_public_files_have_no_testnet_references`.

### Cross-area notes

- `POST /v1/sim/deep` is wired in the API and priced, but core simulation still
  raises `NotImplementedError` for deep mode, so the endpoint currently returns
  503. The MCP/client surface is ready for the anvil-fork implementation.
- Batch-settlement recommendation: **no-go for v2 launch**; revisit as an
  opt-in high-frequency scheme once facilitator support, volume, and durable
  channel storage justify escrow/refund operations.

## Wave 3 — Accept negotiation, API-key surface, monitoring docs

- `src/preflight/mcp/server.py` — every API call (`scan_source`,
  `simulate_transaction`, `pricing`) now sends `Accept: application/json`.
  Added optional `X-Preflight-Key` from `MCP_API_KEY`, falling back to the first
  entry in `PREFLIGHT_API_KEYS`; unset = no header, behavior unchanged. 401/403
  now surface an actionable `ToolError`.
- `client/pay_client.py` — Accept header on both the initial 402 and the paid
  retry, plus dev requests. Added `--key` (precedence: CLI → `PREFLIGHT_API_KEY`
  env → `.env`) and `X-Preflight-Key` on all API requests when configured.
- `public/llms.txt` — documents the optional API-key layer and client/MCP key
  sources; pricing/mainnet facts remain aligned with `public/landing.html`.
- `docs/DISCORD_SETUP.md` — new “GitHub Actions monitoring (no extra accounts)”
  section covering the 15-minute uptime/recovery workflow, the monthly
  settlement-keepalive reminder, and the `DISCORD_WEBHOOK_URL` repo secret.
- Tests: MCP Accept/key-off/key-on/PREFLIGHT_API_KEYS fallback, 401 actionable
  error, and a `/` negotiation mock proving the pricing tool gets JSON while
  `*/*` gets HTML; client Accept/key/precedence tests.

Full offline suite: **139 passed, 5 deselected**.

## Wave 4 — generated discovery, token-risk surface

- `src/preflight/api/discovery.py` — new `build_x402_discovery(cfg)` derives
  resource/payment/pricing/endpoints/mcp from `ROUTE_PRICES` + `Config`; CDP
  credentials select the Coinbase facilitator URL, otherwise the configured
  public facilitator. Exports `router` with `GET /.well-known/x402.json`.
  Static `public/.well-known/x402.json` regenerated from the same builder and
  kept as the reference/fallback; test asserts generated == static.
- `src/preflight/mcp/server.py` — `preflight_token_risk {address}` → POST
  `/v1/token-risk`; Accept/key/dev headers, 402 and 401 ToolErrors, ScanReport
  validation.
- `client/pay_client.py` — `--route token-risk --address 0x…` (same `{address}`
  body and $0.05 price).
- `public/llms.txt` — token-risk route row, trading-agent paragraph, request
  body, MCP tool; facilitator corrected to the CDP URL. `public/landing.html`
  price table also lists token-risk.
- Tests: generated discovery/payment/pricing/router/static-equality and
  token-risk MCP/client routing/payload.

Cross-area request for arbiter: remove the static `@app.get("/.well-known/
x402.json")` handler from `src/preflight/api/app.py` and include the generated
router instead:

```python
from preflight.api.discovery import router as discovery_router
app.include_router(discovery_router)
```

Full offline suite: **161 passed, 6 deselected**.
