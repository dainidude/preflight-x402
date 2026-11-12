# Preflight — Build Coordination (READ FIRST)

**Product**: pay-per-call smart-contract security API. An agent (or dev) POSTs Solidity source, gets back a severity-scoreed, signed security verdict. Payments via **x402** (HTTP 402 + USDC on **Base Sepolia testnet** for now).

## Ownership map — DO NOT edit outside your area

| Area | Owner | Path |
|---|---|---|
| Analysis pipeline, detectors, scoring | **hermes** | `src/preflight/core/**`, `tests/fixtures/**`, `tests/test_core*.py` |
| FastAPI app, x402 payment wiring, pricing, cache, rate limit | **omp** | `src/preflight/api/**`, `tests/test_api*.py` |
| MCP server, discovery files, paying client demo | **crush** | `src/preflight/mcp/**`, `client/**`, `public/**`, `tests/test_mcp*.py` |
| Scaffold, types contract, config, attestation, integration | **opencode (arbiter)** | `src/preflight/types.py`, `config.py`, `attestation.py`, `pyproject.toml`, README |

If you need a change in another area: **stop, and report it in your final summary** instead of editing.

## Hard rules

1. **No new dependencies.** Everything you need is in `pyproject.toml`. If something is truly missing, note it in your summary.
2. Run everything with **`uv run`** from the repo root (e.g. `uv run pytest tests/test_core_scan.py`). Do NOT create your own venv, do NOT run `uv add`/`uv sync` (the arbiter owns the lock).
3. Python 3.12+. Type hints required on public functions. `ruff`-clean style, 100-col lines.
4. **Never commit secrets.** Burner/demo keys go in `.env` (gitignored) only.
5. Tests must pass offline where possible: mark anything needing network with `@pytest.mark.network` and keep it out of the default run.
6. When done: write `docs/STATUS_<area>.md` with what you built, how to run it, and any cross-area requests. Keep it short.

## The interface contract (defined in `src/preflight/types.py` — arbiter owns it)

- `core.pipeline.run_analysis(source: str, contract_name: str | None = None, solc_version: str | None = None) -> ScanReport`
  - Synchronous, CPU-bound. The API layer calls it via `anyio.to_thread.run_sync` (or FastAPI's implicit threadpool for sync route handlers).
  - Must work with solc installed via `py-solc-x` (`solcx.install_solc_pragma` style); auto-install the right version on first use.
  - `ScanReport.verdict` ∈ `looks_ok | review | block`; findings severity ∈ `critical | high | medium | low | info`.
  - Scoring rule (default): any critical/high → `block`; any medium → `review`; else `looks_ok`.
- API surface (omp):
  - `GET /` → service + price list (free). `GET /health` (free).
  - `POST /v1/scan` body `{source, contract_name?, solc_version?}` → **$0.01**, network `base-sepolia`, asset = test USDC `0x036CbD53842c5426634e7929541eC2318f3dCF7e`.
  - `POST /v1/scan/project` body `{files: {name: source}, contract_name?}` → **$0.10** (stretch — ship after /v1/scan works).
  - `POST /v1/address-risk` body `{address}` → **$0.005** static heuristics only (checksum, contract-vs-EOA via optional RPC, known-pattern list). Stretch.
  - **DEV_MODE=1** env bypasses payment (returns `X-Preflight-Dev: bypass` header) so local smoke tests run without a wallet. Default OFF.
  - Free tier: 10 free scans/day per client fingerprint when `FREE_TIER=1`.
  - Cache: SHA-256 of source (after normalize) → report, in-memory LRU (maxsize 512, TTL 1h). Redis optional, absent by default — do not require it.
  - Rate limit: 10 req/min per payer identity, $10/day cap approximation via counting paid calls.
- MCP surface (crush): tool `preflight_check` with input `{source, contract_name?}` → returns the `ScanReport` JSON. The MCP server calls the HTTP API (`PREFLIGHT_API_URL`, default `http://127.0.0.1:8000`) so payments flow through x402; if `DEV_MODE=1` it skips payment. Stdio transport + a `--http` streamable option.
- Client demo (crush): `client/pay_client.py` — generates/loads a burner keypair from `.env` (`CLIENT_PRIVATE_KEY`), tries testnet USDC faucet if balance is 0, then calls `POST /v1/scan` and pays the 402. Must print the full 402 → pay → retry → verdict flow. If the faucet is unreachable, fall back to a `--dev` flag that relies on server DEV_MODE.
- Attestation (arbiter): `attestation.sign_report(report_dict) -> dict | None` — EIP-712 over a report digest using `PREFLIGHT_ATTEST_KEY`; `None` when unset. `ScanReport.attestation` carries it.

## Env vars (all optional except where noted)

```
PREFLIGHT_PAY_TO_ADDRESS=   # receiving address (REQUIRED in prod; any valid address for the demo)
PREFLIGHT_NETWORK=base-sepolia
PREFLIGHT_FACILITATOR=https://x402.org/facilitator   # public testnet facilitator (verify:/verify settle:/settle)
PREFLIGHT_PORT=8000
DEV_MODE=0
FREE_TIER=1
PREFLIGHT_ATTEST_KEY=       # hex private key for EIP-712 report attestations (optional)
CLIENT_PRIVATE_KEY=         # burner key for the paying demo client (client/ only)
PREFLIGHT_API_URL=http://127.0.0.1:8000
RPC_URL=                    # optional public Base Sepolia RPC
```
