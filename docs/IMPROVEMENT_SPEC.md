# Preflight — Improvement & Optimization Spec

Date: 2026-11-13 · Status of service: LIVE on Base mainnet (Modal, ~$5/mo), listed on x402scan,
Bazaar-indexed. Revenue to date: none recorded in docs/metrics. This spec is ordered by
**impact on sales and product quality**, not by engineering aesthetics.

---

## 1. Where the project actually stands

**Real and good:** x402 V2 wiring is genuine (`x402==2.27.0`, CDP facilitator, settle retry
3x + dead-letter journal, serve-then-settle), Slither + solc run for real, `simulation.py` is
substantial (anvil fork, revert decode, gas, ERC-20 honeypot probe), EIP-712 attestations work,
discovery surface (`.well-known/x402.json`, `llms.txt`, landing page, MCP server) is above the
median of the x402 catalog. ~135 tests pass offline.

**Weak and dangerous:** custom detectors are line-regex (no AST/data-flow); the honeypot
detector hard-blocks every standard OpenZeppelin `Pausable` token; `/v1/address-risk` charges
$0.005 for an EIP-55 checksum; Slither's blocking `subprocess.run` (up to 120 s) runs **inside
the event loop** of a single always-on container; ~1,900 lines of leftover agent "fix scripts"
in the repo root will destroy `pipeline.py` if ever executed; `docs/STATUS_api.md` describes a
mocked implementation that no longer exists.

---

## 2. P0 — Trust & reliability (ship before any marketing)

A security product sells on precision and uptime. These are the defects that lose a paying
agent permanently on first contact.

### P0.1 Un-block the event loop
`run_analysis` is async but calls Slither via blocking `subprocess.run`. One slow scan stalls
**every** request on the single `min_containers=1` container — including `/health`, which then
trips the heartbeat alert.
**Fix:** make `run_analysis` sync per the AGENTS.md contract and call it from the route via
`anyio.to_thread.run_sync` (or switch Slither invocation to `asyncio.create_subprocess_exec`).
Add a concurrency cap (semaphore, 2–3 scans) so a burst degrades to queueing, not timeouts.
*Owner: hermes (pipeline) + omp (route call).*

### P0.2 Fix the honeypot false positive
`custom:honeypot` flags owner-only `pause/unpause` as `high` → verdict `block`. That blocks
the majority of legitimate production tokens. A security API that cries wolf on OZ `Pausable`
is worse than no API.
**Fix:** downgrade standalone pausability to `medium` (verdict `review`) unless combined with
a second signal (blacklist mapping, transfer hook gated on owner, fee setters without caps).
Add fixtures: `clean_pausable_oz.sol` must come back `review` or better. *Owner: hermes.*

### P0.3 Fix score dilution
`calculate_score` averages severity weights over `len(findings)` — ten `info` findings dilute
one `critical`. Attested, reproducible scores are a selling point; a gameable score is a
liability. **Fix:** score = max-severity base + diminishing sum of remaining findings (or drop
the numeric score from the public report until it's sound). *Owner: hermes.*

### P0.4 Delete the footguns and stale docs
- Delete `fix_round.py`, `apply_fixes.py`, `fix_implementation.py`, `check_project.py`,
  `status_check.py`, `test_import.py` (repo root). Two of these rewrite `payments.py`/`app.py`
  with old mock content and delete `core/pipeline.py` if run.
- Rewrite `docs/STATUS_api.md` — it still claims payments and the pipeline are mocked.
- Fix `state.py:51` `json.JSONEncodeError` (doesn't exist → NameError on the error path).
- Commit `uv.lock` (it's gitignored; the DR runbook's "clone + uv sync" is currently not
  reproducible). *Owner: arbiter.*

### P0.5 Test the money path offline
The settle-retry loop, dead-letter write, and V2 header shapes are exercised only by one
network-gated test. Add offline tests with a **mocked facilitator** (httpx transport mock):
verify→settle happy path, settle fail×3 → dead-letter row, V1-fallback parse, replay via
`settle_repair.py`. *Owner: omp.*

---

## 3. P1 — Product changes that actually create sales

### P1.1 Pivot the flagship to **address-first** scanning (biggest lever)
Today's primary input is Solidity **source**. The agents that hold funded wallets are
**trading agents**, and they hold **token addresses**, not source. The repo already has the
hard part built: the anvil-fork honeypot probe and tax measurement in `simulation.py`.

New route: `POST /v1/token-risk {address}` — **$0.05**
1. Fetch verified source from Basescan/Etherscan API via `httpx` (free key, no new deps);
   if available, run the full static pipeline on it.
2. Always run the live fork probe: buy/sell round-trip, transfer tax %, honeypot verdict,
   owner privileges detected from bytecode heuristics.
3. Return the same attested `ScanReport`.

This turns Preflight from "audit my code" (tiny, source-rich, payment-poor audience) into
"can I safely trade this token" (the one agent audience that demonstrably spends USDC).
Replace `/v1/address-risk` with this, or keep it as the free teaser tier.
*Owner: hermes (probe reuse) + omp (route) + crush (MCP tool `preflight_token_risk`).*

### P1.2 Make `/v1/address-risk` honest or kill it
Charging $0.005 for a checksum validation is the fastest way to a "scam service" reputation
on x402scan reviews. Implement the contracted minimum (RPC `eth_getCode` contract-vs-EOA,
known-pattern list, deployer age) or delist the route from discovery files until P1.1 lands.

### P1.3 Put the free trial in the 402 itself (conversion funnel)
The free tier requires `X-Preflight-Free: trial` — correct for crawler validation, but no
agent will discover it organically. Add to the 402 JSON body's `error` field and to
`info.x-guidance`: *"First 10 scans/day free: retry with header `X-Preflight-Free: trial`"*.
A 402 that teaches the free path converts; a bare 402 bounces. *Owner: omp.*

### P1.4 Generate discovery files from code
`public/.well-known/x402.json` (832 hand-written lines) duplicates prices, pay_to, network,
and facilitator — and is already drifting (it names x402.org while prod settles via CDP).
Generate it at startup from `ROUTE_PRICES` + config; serve, don't hand-maintain.
*Owner: crush + omp.*

### P1.5 Distribution (zero code, highest ROI per hour)
- Publish the MCP server to registries: Smithery, PulseMCP, mcp.so, the official MCP registry.
  Agents find tools through MCP catalogs far more than through x402 directories today.
- Keep the Bazaar listing alive (`scripts/index_bazaar.py` monthly cron on Modal — automate it;
  the 30-day drop rule is currently enforced by memory).
- Free verification endpoint `GET /v1/verify?digest=…` + publish the attestor address — makes
  the EIP-712 attestation a *checkable* claim, which is marketing other catalog entries can't copy.
- Content loop: a daily Modal cron scans 3 trending Base tokens, posts verdicts to the Discord
  webhook / a public feed page. Demonstrated verdicts are the only ad that works for a scanner.

### P1.6 Contract compliance cleanup
- Implement the promised $10/day per-payer cap (count paid calls; currently only 10 req/min).
- Update AGENTS.md: network is Base **mainnet**, routes now include `/v1/sim` ($0.05) and
  `/v1/sim/deep` ($0.25), free tier is opt-in-by-header. The contract doc should describe the
  shipped product. *Owner: arbiter.*

---

## 4. P2 — Engineering hygiene

- Cache: record cache hits in metrics (they currently vanish from `record_scan`); consider a
  discounted price or `X-Preflight-Cache: hit` header so payers know they bought a cached result.
- Remove dead code: unused module-level globals in `cache.py`, `_run_sync` in the pipeline,
  the phantom `X-Preflight-Key` auth layer the MCP server references but the API never reads.
- Deduplicate `SEVERITY_ORDER` (`types.py` is the owner).
- Fingerprint hardening: free-tier fingerprint = UA+XFF hash, trivially rotated. Acceptable at
  10 free scans; revisit if free-tier abuse shows in metrics.
- Pin top-level deps (`web3`, `mcp` have already drifted a major version past the spec floor);
  committing `uv.lock` (P0.4) covers prod, pins cover intent.
- 2–3 more detector fixtures per detector, asserting **finding-level** precision, not just verdict.

---

## 5. Will this fail or succeed?

### Why it fails (base case, as currently positioned)
1. **The buyer doesn't exist yet.** The product needs an agent that (a) possesses Solidity
   source, (b) holds a funded USDC wallet, (c) autonomously decides to pay for security review.
   That intersection is approximately empty in 2026. Humans with source use Slither directly —
   for free. The x402 catalog has 29k+ entries and a curated discovery slice; listing ≠ demand.
2. **Thin moat.** The core verdict is Slither (free OSS) + five regex detectors. Anyone can
   wrap Slither behind x402 in a weekend; several catalog entries already do. The defensible
   parts (fork simulation, attestation) are not what the flagship route sells.
3. **The trust paradox.** Security verdicts are a reputation business. A $0.01 verdict with a
   "`looks_ok` is not an audit" disclaimer and a known FP that blocks standard OZ tokens
   carries negative signaling value for anything with money at stake. P0.2 is existential.
4. **No distribution engine.** No feed of demonstrated verdicts, no MCP registry presence,
   free trial hidden behind an undiscoverable header. Nothing currently *pulls* a buyer in.

### Why it succeeds (conditional case)
1. **Infrastructure quality is real.** Settle retries, dead-letter journal, attestations,
   Modal always-on at ~$5/mo — this is a top-decile x402 implementation. At near-zero carry
   cost it can idle as a call option on the agentic-commerce market growing into it.
2. **The pivot path exists inside the repo.** The anvil honeypot probe + tax measurement is
   genuinely valuable to trading agents — the only agent cohort with funded wallets today.
   Repositioned address-first (P1.1), Preflight sells to who has money, not who has source.
3. **Early-mover listing + attestation** is a durable differentiator *if* verification is made
   public (P1.5): "cryptographically accountable verdicts" is a claim regex-wrapper clones
   can't make without doing the work.

**Verdict:** as positioned (source-in, $0.01, regex+Slither), it fails commercially — expect
test-traffic revenue only. With P0 trust fixes + the P1.1 address-first pivot + MCP-registry
distribution, it has a real shot at being one of the few x402 services with organic repeat
buyers. Keep running either way; the carry cost is a coffee.

---

## 6. Three better x402 projects

Ranked by (buyer already has a wallet) × (call frequency) × (buildable with this stack).

### 1. Token & wallet intelligence feed for trading agents
`POST /v1/token {address}` → liquidity depth & lock status, holder concentration, deployer
history/reputation, honeypot probe, tax %; `POST /v1/wallet {address}` → PnL, bot-likeness,
counterparty risk. **Why better:** the buyer (trading agent) has a funded wallet *by
definition* and calls per trade decision — hundreds of calls/day per agent vs. a dev's one
scan per deploy. 70% of Preflight's hard parts (fork sim, x402 plumbing, attestation) reuse
directly. Price $0.02–0.10/call; one active trading agent ≈ $5–30/day.

### 2. Web content extraction for agents (pay-per-fetch)
`POST /v1/fetch {url}` → clean markdown, JS-rendered, anti-bot handled; `POST /v1/search`
proxy. **Why better:** this is the highest-frequency need every agent has, with proven
willingness to pay (Firecrawl/Jina charge subscriptions for exactly this). x402's actual
killer feature is **signup-free metered access** — an agent mid-task can't fill in a billing
form, but it can pay $0.005 per fetch. Volume business: thousands of calls/day achievable,
no trust/liability problem, verdictless — nothing to be wrong about.

### 3. Ephemeral code-execution sandbox per call
`POST /v1/run {language, code, stdin?}` → stdout/stderr/exit code from a disposable
container, $0.01/run, $0.05 with network. **Why better:** every coding/analysis agent needs
a sandbox and most hosts won't give them one; per-call pricing matches per-call usage
perfectly. The Modal deployment knowledge from this project *is* the product (sandboxed
containers, baked images, volume state). High frequency, zero inventory, compute-arbitrage
margins, and the x402 integration is already written in this repo.

**Common thread:** Preflight sells *judgment* (hard to trust, low frequency). All three sell
*utility* (easy to verify, high frequency) to buyers who already hold funded wallets. In a
market this young, sell picks to the only miners with money: trading and coding agents.
