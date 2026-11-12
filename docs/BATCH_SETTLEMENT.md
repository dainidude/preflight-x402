# Batch settlement for Preflight — design (v2 decision, no implementation)

Status: **proposal only**. Nothing in this document is wired into the API,
client, or MCP server. `exact` (EIP-3009) remains the shipping scheme.

## Why consider it

Preflight's paid tickets are small ($0.005–$0.25) and the product is aimed at
agents that may call `/v1/scan` or `/v1/sim` thousands of times. With `exact`,
every call is an independent EIP-3009 authorization that the facilitator must
verify and settle onchain. The payer does not pay gas, but the facilitator and
receiver pay one settlement per call in operational overhead. The x402
`batch-settlement` scheme replaces that with a payment channel: one onchain
deposit, off-chain cumulative vouchers per request, and periodic batched
redemption.

## How the paid flow changes

| Step | Today (`exact`) | With `batch-settlement` |
|---|---|---|
| First call | 402 → sign EIP-3009 transfer → facilitator settles one transfer | 402 advertises the batch scheme → client opens/tops up a channel (EIP-3009 or Permit2 deposit) and signs voucher #1 |
| Later calls | Repeat 402 → sign → settle each time | 402 → sign a new **cumulative** voucher (no onchain tx) → serve immediately |
| Money movement | Per call, at request time | Deposit up front into escrow; receiver claims voucher batches, then sweeps to `pay_to` |
| Latency | Facilitator verify + settle per call | Local voucher verify; settle is asynchronous |
| Failure mode | 402/invalid payment | Corrective 402 when the voucher is behind the server's cumulative state; channel top-up or recovery |

A channel is identified by an EIP-712 `ChannelConfig`
(`payer`, `payerAuthorizer`, `receiver`, `receiverAuthorizer`, `token`,
`withdrawDelay`, `salt`). The voucher is a signed cumulative ceiling
(`channelId`, `maxClaimableAmount`); the server only serves if the new ceiling
equals `previous charged cumulative + this route's price` and does not exceed
the deposited balance. The route `price` remains a per-request maximum.

## Server-side changes (API, not this area)

1. **Scheme registration per network, route-level choice.** The API currently
   registers only `ExactEvmServerScheme` and hardcodes `scheme="exact"` in
   `build_requirements`. Add a second registration for
   `BatchSettlementEvmScheme(receiver, config)` on `eip155:8453`, and select the
   scheme per route (or advertise both accepts and let the client choose).
   `batch-settlement` is not a drop-in replacement for `exact`; it needs its own
   `PaymentRequirements` (`scheme: "batch-settlement"`, `extra.minDeposit`,
   `withdrawDelay`, channel-state hints) and lifecycle hooks.
2. **Durable channel storage and admission locks.** Use `FileChannelStorage`
   (or Redis) for the voucher store, not `InMemoryChannelStorage`, so
   outstanding vouchers survive restarts and multi-instance deploys. Per-channel
   admission locks are required; otherwise concurrent requests on one channel
   can double-spend the voucher headroom.
3. **Receiver authorizer key.** Configure `receiver_authorizer_signer` and
   `withdraw_delay` (15 min–30 days; start at 1 day). This key signs claims and
   cooperative refunds. It is a hot key with custody implications and needs the
   same secret-handling discipline as `PREFLIGHT_ATTEST_KEY`.
4. **Channel manager.** Run claim/settle/refund loops outside the request path
   (`claim_interval_secs` ≈ 60, `settle_interval_secs` ≈ 300,
   `refund_interval_secs` ≈ 3600, `max_claims_per_batch` 100). Claims must run
   comfortably inside `withdraw_delay`, or a payer's timed withdrawal can beat
   the claim and unclaimed revenue returns to the payer.
5. **Pricing map.** Keep `ROUTE_PRICES` unchanged. Each paid request increments
   the channel's cumulative voucher by that route's price. Mixed traffic
   (`/v1/scan` $0.01 and `/v1/sim/deep` $0.25) is fine on one channel because the
   server checks the exact cumulative delta per request. Free-tier and
   `DEV_MODE` requests must bypass voucher accounting entirely.
6. **Facilitator support.** Confirm the production facilitator advertises
   `batch-settlement` (and a `receiverAuthorizer`, or supply our own). Without
   that, deposits/claims cannot be relayed.

## Client-side changes (this area, if approved)

`client/pay_client.py` would register both schemes on the same `x402Client`
(`register("eip155:*", exact)` and `register("eip155:*", batch)`), so the client
picks the accept the server offers. Batch setup needs an RPC-capable signer
(`EthAccountSignerWithRPC`), a `BatchSettlementDepositPolicy(deposit_multiplier=5)`,
and persistent `FileClientChannelStorage` so a restart does not strand channel
state. First paid call performs a deposit (`5×` request price by default, or the
server's `extra.minDeposit`, typically `10×`); subsequent calls are
voucher-only. Add a `--refund` command (full or partial) and surface channel
balance/idle state. MCP tools would not change: they already speak HTTP, and the
paying agent supplies the scheme.

## Pricing → vouchers

For fixed-price Preflight routes the mapping is direct:

```
voucher_n = voucher_(n-1) + price(route_n)     (capped by channel deposit)
```

Examples: ten `/v1/scan` calls consume 100000 base units ($0.10); one
`/v1/sim/deep` consumes 250000 ($0.25). A `$1.00` minimum deposit (server hint)
covers 100 scans or 4 deep sims before a top-up. Dynamic discounts would need
settlement overrides; Preflight's fixed menu does not.

## Risks

- **Escrow custody.** User USDC sits in an onchain contract under our
  `receiverAuthorizer`. A compromised authorizer key can claim up to the
  deposited balance; a missed claim window lets a payer withdraw first. This is
  real-money custody, not a payment-authorization relay like `exact`.
- **Refund UX.** Unused channel balance must be claimed then cooperatively
  refunded (client-initiated, server-signed) or withdrawn after
  `withdrawDelay`. Agents that crash or rotate keys leave funds idle; support
  needs a refund/recovery path and clear docs.
- **Operational surface.** Durable storage, Redis/file locks, background
  claim/settle jobs, RPC/nonce management, deposit monitoring, and channel
  reconciliation are new always-on concerns. `exact` has none of them.
- **Minimum deposit friction.** Default `10×` (or `5×` multiplier) turns a
  $0.05 call into a $0.50–$1.00 first payment. Acceptable for agents, hostile
  for one-off users; keep `exact` for the latter.
- **Corrective 402s and retries.** A stale/recovered channel produces a 402
  with channel state; clients must resync and re-sign. MCP tools and simple
  scripts that treat every 402 as "payment rejected" would regress unless the
  client SDK handles recovery.
- **Facilitator dependency.** The scheme's deposit/claim/settle path is only as
  available as the facilitator's batch support; it is a newer code path than
  exact EIP-3009.

## Recommendation

**No-go for the v2 launch.** Keep `exact` as the default and ship deep
simulation first; batch settlement adds escrow custody, refund support, durable
channel state, and always-on claim/settle operations whose cost is not justified
by $0.005–$0.25 tickets at launch volume. Revisit as an **opt-in** scheme for
high-frequency agent accounts once (a) the facilitator advertises
`batch-settlement` on Base mainnet, (b) we see sustained per-payer volume where
per-call settlement overhead matters, and (c) a durable channel store plus
refund runbook are funded. Until then, advertise both schemes in discovery but
serve `exact`; a dual-accept rollout is the lowest-risk migration when the
go/no-go flips.