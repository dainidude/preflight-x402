#!/usr/bin/env python3
"""Trigger CDP Bazaar indexing with one settled paid call.

Per docs.cdp.coinbase.com/x402/seller/get-discovered: a route gets indexed
after a successful settled payment through the CDP Facilitator. This script
pays $0.01 for a /v1/scan against the production URL and reports the bazaar
extension status from the EXTENSION-RESPONSES header.

Preconditions:
  * CDP_API_KEY_ID / CDP_API_KEY_SECRET in .env (server side redeployed)
  * CLIENT_PRIVATE_KEY funded with test USDC
  * the production API running in paid mode

Usage:  uv run python scripts/index_bazaar.py [--url https://...]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))

import httpx  # noqa: E402

import pay_client  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="https://preflight--preflight-api-api.us-east.modal.run",
        help="Base URL of the production API",
    )
    args = parser.parse_args()

    key = pay_client.load_env(pay_client.ENV_FILE).get("CLIENT_PRIVATE_KEY")
    if not key:
        print("CLIENT_PRIVATE_KEY missing from .env")
        return 1

    url = f"{args.url.rstrip('/')}/v1/scan"
    payload = pay_client.scan_payload(pay_client.SAMPLE_SOURCE, "VulnerableVault")

    async with httpx.AsyncClient(timeout=180.0) as client:
        first = await client.post(url, json=payload)
        if first.status_code == 200 and first.headers.get("x-preflight-free-tier"):
            print("Server granted the free tier — run with FREE_TIER exhausted or use a fresh fingerprint.")
            return 1
        if first.status_code != 402:
            print(f"Expected 402, got {first.status_code}: {first.text[:200]}")
            return 1

        reqs = pay_client.parse_payment_required(pay_client._headers_dict(first.headers), first.content)
        signed = pay_client.create_payment(reqs, key)
        retry = await client.post(url, json=payload, headers=pay_client.payment_headers(signed))

    print(f"paid retry: {retry.status_code}")
    settle = pay_client.parse_settle_response(pay_client._headers_dict(retry.headers))
    print(f"settle success: {settle and settle.get('success')} | tx: {(settle or {}).get('transaction', '')}")

    ext_header = retry.headers.get("extension-responses")
    if ext_header:
        try:
            ext = json.loads(base64.b64decode(ext_header))
            bazaar = ext.get("bazaar") or {}
            print(f"bazaar status: {bazaar.get('status')} (reason: {bazaar.get('rejectedReason')})")
        except Exception as exc:
            print(f"(could not decode EXTENSION-RESPONSES: {exc})")
    else:
        print("no EXTENSION-RESPONSES header — bazaar submission not reported by facilitator")

    print("Indexing takes up to ~15 minutes. Verify afterwards with:")
    print("  curl -s 'https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources' | head")
    return 0 if (settle or {}).get("success") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
