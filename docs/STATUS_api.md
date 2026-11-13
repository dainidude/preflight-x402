# Preflight API Status Report

Status: **LIVE on Base mainnet** (real USDC, CDP facilitator with KYT/OFAC).

## What exists (current, real — replaces all earlier reports)

- `app.py` — FastAPI with content-negotiated root (HTML landing for scrapers, JSON for agents),
  five paid routes (scan $0.01, sim $0.05, sim/deep $0.25, scan/project $0.10,
  address-risk $0.005), EIP-712 attestation on every report, per-agent attribution.
- `payments.py` — real x402 V2 middleware on the `x402` package: spec-shaped 402
  (PaymentRequired v2 + ResourceInfo + full-object base64 header), Bazaar declarations,
  opt-in free tier (`X-Preflight-Free: trial`, IP-keyed so quotas can't be rotated),
  3-attempt settle retry with dead-letter journal, optional `X-Preflight-Key` layer
  (default off), $10/day per-payer cap, rate limits, revenue booked from route prices.
- `metrics.py` — durable counters on the Modal Volume (revenue force-flushes past the
  throttle), first/last settle timestamps, per-agent masked top-revenue attribution.
- `state.py` — DurableState (atomic JSON snapshots) + settle dead-letter storage.
- `cache.py` — source-hash LRU (512 entries, 1h TTL); keys include contract_name/solc_version.
- `ratelimit` — 10 req/min per payer; free trials: 10/day per IP+UA identity.

## History

Earlier iterations of this file described a mocked payment layer and a mock analysis
pipeline (wave 1, since replaced entirely). Nothing mocked ships today: verify/settle
run through Coinbase's CDP facilitator, and scans run solc + Slither for real.
