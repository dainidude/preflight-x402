# Preflight Core Analysis Pipeline — Status

## What was built

### Core modules (`src/preflight/core/`)
- **`pipeline.py`** — `run_analysis(source, contract_name, solc_version) -> ScanReport`
  - Parses pragma, auto-installs solc via py-solc-x (pre-installed: 0.8.20/0.8.24/0.8.26)
  - Runs Slither via subprocess with `--json` output, robust to exit codes
  - Runs 5 custom DeFi detectors
  - Returns typed `ScanReport` with verdict, score, findings, metadata

- **`scoring.py`** — Verdict logic & scoring
  - `determine_verdict(findings)`: critical/high → `block`, medium → `review`, else `looks_ok`
  - `calculate_score(findings)`: 0–100 weighted by severity × confidence
  - `merge_findings()`: deduplicates Slither + custom by finding ID

- **`detectors/custom.py`** — 5 custom DeFi detectors (text/AST heuristics)
  - `fee-on-transfer`: fee variable patterns, balance comparisons
  - `unguarded-initializer`: initializer without access control; UUPS `initialize()` missing modifier
  - `oracle-freshness`: `latestRoundData()` without `updatedAt`/staleness check
  - `missing-zero-check`: constructor/initializer params assigned without `address(0)` validation
  - `honeypot`: owner-controlled pause, blacklist/whitelist, `_beforeTokenTransfer` hooks

- **`report.py`** — `build_scan_report()` assembles final `ScanReport` with:
  - `source_sha256` (normalized source, cache key)
  - `scanner_versions` (slither, solc, custom)
  - `scanned_at` ISO-8601 UTC
  - `attestation` placeholder (filled by API layer)

- **`project.py`** — `run_analysis_project(files, contract_name, solc_version)`
  - Writes all files to temp dir, runs Slither on main file with `--allow-paths`
  - Runs custom detectors on combined source
  - Shares scoring/report logic with single-file pipeline

- **`__init__.py`** — exports `run_analysis`, `run_analysis_project`

### Test fixtures (`tests/fixtures/`)
- `clean_erc20.sol` — minimal ERC20, expects `looks_ok`
- `reentrancy_vulnerable.sol` — classic reentrancy, expects `block` (Slither high)
- `unguarded_uups.sol` — UUPS with unguarded `initialize`, expects `block`/`review` (custom high)
- `oracle_no_freshness.sol` — Chainlink without freshness check, expects `review` (custom medium)
- `honeypot_pausable.sol` — owner pause/blacklist, expects `block` (custom high)

### Tests (`tests/test_core_pipeline.py`)
- 16 tests covering all fixtures, edge cases, custom detectors, project analysis
- All pass offline with pre-installed solc
- Fixtures defined in `tests/conftest.py`

## How to run tests
```bash
cd /home/dude/Projects/preflight-x402
uv run pytest tests/test_core_pipeline.py -v
# or all offline tests:
uv run pytest tests/ -m "not network" -v
```

## Cross-area requests
- None — API layer (omp) already calls `core.pipeline.run_analysis` via threadpool; MCP (crush) calls HTTP API; arbiter owns types/contract.

---

# Brief 2 — RPC transaction simulation + false-positive tuning

## What was built

### Core module (`src/preflight/core/simulation.py`)
- `run_simulation(req: SimRequest) -> SimReport`
  - Checksum-validates `to`; invalid address yields `heuristic:validation` medium finding.
  - `eth_getCode(to)` check: empty code + calldata → `sim:call-to-eoa` low finding; empty code with no calldata is noted as a plain value transfer.
  - `eth_call` against `RPC_URL` (default `https://sepolia.base.org`) with a state-override balance for the simulated sender when `value_wei > 0`.
  - Reverts are caught and decoded: `Error(string)` (`0x08c379a0`) and `Panic(uint256)` (`0x4e487b71`) produce a `sim:revert` high finding → `block` verdict.
  - Optional `req.source` runs the existing custom detectors and merges findings.
  - Verdict and score are computed by `core.scoring.determine_verdict` / `calculate_score`; `simulator = "rpc-ethcall:v1"`.
  - All RPC calls are wrapped in try/except: failures add a note + `sim:rpc-failure` info finding and still return a structured `SimReport`.
  - Web3 instance is built through `_create_web3()` so tests can monkeypatch the RPC layer without touching address validation.

### Test fixtures (`tests/fixtures/`)
- `clean_reentrancyguard_vault.sol` — manual ReentrancyGuard + checks-effects-interactions withdraw pattern; tuned to `looks_ok`.
- `clean_oracle_fresh.sol` — Chainlink `latestRoundData` with `updatedAt` + heartbeat + `answeredInRound` staleness checks; tuned to `looks_ok`.

### Tests
- `tests/test_simulation.py` — 13 tests (12 offline + 1 `@pytest.mark.network`):
  - revert decoding, invalid address, contract call, EOA with data, value-transfer balance override, Error/Panic reverts, RPC failure handling, custom sender, source-detector merging.
- `tests/test_core_false_positive.py` — asserts both new clean fixtures return `looks_ok` and that the four existing vulnerable fixtures keep their expected verdicts.
- `tests/conftest.py` — added fixtures for the two new clean sources.

## Test results
```bash
uv run pytest tests/ -m "not network" -v
```
- **94 passed, 4 deselected (network), 1 warning**

## Cross-area requests
- None. The simulation module consumes the `SimRequest`/`SimReport` contract from `src/preflight/types.py` and leaves attestation/API wiring to the arbiter/omp areas.

---

## v2 deep sim

### What was built

#### `src/preflight/core/simulation.py`
- Split into quick (`rpc-ethcall:v1`) and deep (`anvil-fork:v1`) modes driven by `req.mode`.
- Deep mode spawns a local anvil fork against `https://mainnet.base.org` (or `RPC_URL`) per request:
  - funds + impersonates the simulated sender with `anvil_setBalance` / `anvil_impersonateAccount`
  - runs `eth_estimateGas` and mines the tx via `eth_sendTransaction` for real `gas_used`
  - records `balance_delta_wei` (sender balance after − before)
  - decodes reverts (`Error(string)`, `Panic(uint256)`) via `eth_call`
  - traces internal calls with `debug_traceTransaction` + `callTracer`, formatted as `to-short → 0xsel4(...)` and capped at 10
- If anvil is missing or fails to start, deep mode falls back to quick results and adds a `sim:deep-unavailable` low finding + note.
- Anvil binary discovery checks `~/.config/.foundry/bin` and `~/.foundry/bin` in addition to `PATH`.
- Process lifecycle is wrapped in try/finally with terminate/kill and a 90s overall timeout.

#### Core bugfix sweep (`src/preflight/core/`)
- `detectors/custom.py`:
  - Added `interface_ranges` to `DetectorContext` and skip interface blocks / abstract function declarations / comment lines in all detectors, eliminating false positives on interface-only and abstract sources.
  - `run_all_custom_detectors` accepts a `str` or `DetectorContext` and auto-computes ranges when missing.
- `pipeline.py` + `project.py`:
  - `_run_slither_on_source` / `_run_slither_on_project` now return a status note on timeout / missing slither and degrade to custom-detectors-only instead of silently returning empty findings.
  - Added Slither `critical` → `critical` severity mapping.
- `scoring.py`: no bug found; added regression tests proving high findings always produce a positive score and high scores exceed medium scores.

#### Tests
- `tests/test_simulation.py` — added deep-mode offline fallback test and a network deep test against Base mainnet USDC.
- `tests/test_core_detector_edgecases.py` — interface-only, abstract virtual functions, empty contract, multiple pragmas, unbounded `>=0.8.0` pragma.
- `tests/test_core_scoring.py` — verdict rules and score monotonicity/extremes.

### Foundry
- Installed via `curl -L https://foundry.paradigm.xyz | bash` + `foundryup`.
- Verified: `anvil Version: 1.9.0`.

### Test results
```bash
uv run pytest tests/ -m "not network" -q
```
- **124 passed, 5 deselected (network), 1 warning**

Network-marked deep test also passes (Base mainnet fork via anvil).

### Bugs fixed
- Detector false positives on interface-only / abstract / comment-only sources.
- Slither timeout/missing binary now surfaces a note and degrades gracefully.
- Missing `critical` severity mapping from Slither.
- Anvil spawn args corrected (removed invalid `--fork-block-number latest` and `--block-time 0`).

### Cross-area requests
- None.
---

# Wave 4 — P0 trust fixes in core

## P0.1 — Un-block the event loop
- `src/preflight/core/pipeline.py`: `run_analysis` is now a synchronous function.
- `src/preflight/core/project.py`: `run_analysis_project` is now a synchronous function.
- Added `threading.Semaphore(3)` (`_SCAN_SLOTS`) in `pipeline.py` and acquired it around slither/solc work in both functions so bursts queue instead of exhausting the container.
- Updated `tests/test_core_pipeline.py`, `tests/test_core_false_positive.py`, and `tests/test_core_detector_edgecases.py` to call the sync functions directly.

## P0.2 — Honeypot pausable downgrade
- Tuned `src/preflight/core/detectors/custom.py` so standalone owner-controlled pausability is `medium` (verdict `review`).
- Pausability escalates to `high` only when combined with a second owner-controlled transfer-restriction signal:
  - blacklist/allowlist mapping with owner-only mutators,
  - `_beforeTokenTransfer` gating on owner/blacklist,
  - owner-only fee/tax setters without a cap.
- Added fixtures:
  - `tests/fixtures/clean_pausable_oz.sol` — standalone pausable, expected `review`.
  - `tests/fixtures/honeypot_pausable_blacklist.sol` — pausable + blacklist, expected `block`.
- Updated tests in `tests/test_core_pipeline.py` and `tests/test_core_false_positive.py` to assert the new severity-level behavior.

## P0.3 — Score dilution fix
- Rewrote `calculate_score` in `src/preflight/core/scoring.py`:
  - Base score is the max-severity finding (critical 95, high 80, medium 50, low 20, info 5).
  - Additional findings add diminishing contributions (`weight / (rank + 1)`).
  - Total is capped at 99 so many low findings cannot mask a critical.
- Added regression tests ensuring a critical alone scores ≥ 90 and a critical plus 10 infos stays ≥ 90.
- Updated existing scoring tests for the new scale.

## Test results
```bash
uv run pytest tests/ -m "not network" -q
```
- **166 passed, 6 deselected (network), 1 warning**

## Cross-area requests
- None.

---

# Wave 5 — E1 verify endpoint + E2 trending-token verdicts

## E1 — Public attestation verification
- Added `src/preflight/core/verify.py` with `verify_attestation(digest, attestation)`.
  - Wraps the arbiter-owned `attestation.verify_report()` EIP-712 verifier.
  - Returns `{valid, signer, verdict, source_sha256, checked_at}` on success,
    or `{valid: false, reason: ...}` on failure.
- Published the live attester address in `public/llms.txt`:
  `Attestation signer: 0x3779bb827AB1F32A317fD30157Ac03Dc5CCA88aa — verify at GET /v1/verify`.
- `GET /v1/verify` was already present in `FREE_ROUTES` and wired in `src/preflight/api/app.py`.
- Tests in `tests/test_core_verify.py`: valid round-trip, tampered digest, wrong signer,
  missing fields.

## E2 — Trending-token verdicts in the daily digest
- Added `src/preflight/core/trending.py`:
  - `get_trending_base_tokens(n=3)` fetches top Base tokens from GeckoTerminal's
    free pools API, looks up symbols, and returns contract addresses.
  - `scan_trending_tokens(n=3)` runs `run_token_risk` on each token via in-process
    import (no HTTP round-trip), prepending `Token: SYMBOL (address)` to report notes.
  - `format_trending_cards(reports)` produces Discord one-liners
    (`✅/⚠️/🚨 SYMBOL: verdict — top finding`).
- Wired into `deploy/modal_app.py::daily_digest`: after the metrics summary, scans
  3 trending tokens and appends the verdict cards to the Discord message.
- Tests in `tests/test_core_trending.py`: mocked trending source + mocked
  `run_token_risk`, format assertions, and a network-marked real-source test.

## Test results
```bash
uv run pytest tests/ -m "not network" -q
```
- **182 passed, 7 deselected (network), 1 warning**

Note: `tests/test_http_surface.py` hangs in the full-suite run because its `/v1/bundle` tests perform live RPC / anvil work without `@pytest.mark.network`. Excluding that file yields the count above; the E1/E2 tests pass cleanly.

## Cross-area requests
- None.
