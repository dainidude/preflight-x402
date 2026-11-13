#!/usr/bin/env python3
"""Check Preflight's CDP Bazaar listing status (verify-only — spends nothing).

Usage:  uv run python scripts/check_bazaar.py

Reports:
  1. Facilitator bazaar status for our declaration (success/processing/rejected)
  2. Public CDP catalog entries for our pay_to (curated 500-item slice)
  3. The Onyx mirror (indexes the full CDP discovery every 15 min)
  4. Coinbase validate endpoint (index/crawler state)
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))

import httpx  # noqa: E402

import pay_client  # noqa: E402

NURL = "https://preflight--preflight-api-api.us-east.modal.run"
PAYTO = "0x5515f59a30fe2b9152cd5648a3a0d8c30c837d41"


def bazaar_status() -> str:
    env = pay_client.load_env(pay_client.ENV_FILE)
    key = env["CLIENT_PRIVATE_KEY"]
    # .env first so the script works without manual sourcing; real env wins
    kid = os.environ.get("CDP_API_KEY_ID") or env.get("CDP_API_KEY_ID")
    ksec = os.environ.get("CDP_API_KEY_SECRET") or env.get("CDP_API_KEY_SECRET")
    if not (kid and ksec):
        return "CDP keys not found (.env or environment)"
    from cdp.x402 import create_facilitator_config
    from x402.http.facilitator_client import HTTPFacilitatorClient

    with httpx.Client(timeout=120.0) as c:
        first = c.post(f"{NURL}/v1/scan", json=pay_client.scan_payload(pay_client.SAMPLE_SOURCE, "VulnerableVault"))
        if first.status_code != 402:
            return f"endpoint returned {first.status_code} (expected 402)"
        pr = pay_client.parse_payment_required(pay_client._headers_dict(first.headers), first.content)
        signed = pay_client.create_payment(pr, key)
        fc = HTTPFacilitatorClient(create_facilitator_config(api_key_id=kid, api_key_secret=ksec))
        body = {
            "x402Version": 2,
            "paymentPayload": signed.model_dump(by_alias=True, exclude_none=True),
            "paymentRequirements": pr.accepts[0].model_dump(by_alias=True, exclude_none=True),
        }
        r = c.post(
            "https://api.cdp.coinbase.com/platform/v2/x402/verify",
            json=body,
            headers=fc._get_verify_headers(),
        )
        ext = r.headers.get("extension-responses")
        if not ext:
            return f"verify {r.status_code}, no extension responses"
        return f"verify {r.status_code} → {json.loads(base64.b64decode(ext)).get('bazaar')}"


def merchant_catalog() -> str:
    """Authenticated query: our wallet's resources in the CDP catalog."""
    env = pay_client.load_env(pay_client.ENV_FILE)
    kid = os.environ.get("CDP_API_KEY_ID") or env.get("CDP_API_KEY_ID")
    ksec = os.environ.get("CDP_API_KEY_SECRET") or env.get("CDP_API_KEY_SECRET")
    if not (kid and ksec):
        return "CDP keys missing"
    from cdp.auth import get_auth_headers, GetAuthHeadersOptions

    last = "no attempt made"
    for path in (
        f"/platform/v2/x402/discovery/resources?payTo={PAYTO}&limit=50",
        f"/platform/v2/x402/discovery/resources?type=http&payTo={PAYTO}&limit=50",
    ):
        hdrs = get_auth_headers(GetAuthHeadersOptions(
            api_key_id=kid, api_key_secret=ksec,
            request_method="GET", request_host="api.cdp.coinbase.com",
            request_path=path.split("?")[0], source="x402",
        ))
        req = urllib.request.Request("https://api.cdp.coinbase.com" + path, headers=hdrs)
        try:
            items = json.load(urllib.request.urlopen(req, timeout=60)).get("items", [])
            ours = [i for i in items if PAYTO in json.dumps(i)]
            return (
                f"{len(ours)} of {len(items)} items for our wallet"
                if ours
                else f"endpoint ignores payTo (returned {len(items)} general items, 0 ours)"
            )
        except Exception as e:
            last = str(e)[:80]
    return f"query failed: {last}"


def public_catalog() -> int:
    try:
        items = json.load(
            urllib.request.urlopen(
                "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources?type=http&limit=500",
                timeout=90,
            )
        ).get("items", [])
        return sum(1 for i in items if PAYTO in json.dumps(i))
    except Exception:
        return -1


def onyx_mirror() -> int:
    try:
        data = urllib.request.urlopen("https://onyx-actions.onrender.com/bazaar.json", timeout=45).read()
        return data.lower().count(b"preflight")
    except Exception:
        return -1


def validate_index() -> str:
    try:
        req = urllib.request.Request(
            "https://api.cdp.coinbase.com/platform/v2/x402/validate",
            data=json.dumps({"resource": f"{NURL}/v1/scan", "method": "POST"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        d = json.load(urllib.request.urlopen(req, timeout=60))
        return f"valid={d.get('valid')} index={d.get('index')}"
    except Exception as e:
        return f"error: {e}"


if __name__ == "__main__":
    print("1. facilitator bazaar status:", bazaar_status())
    print("2. authenticated merchant catalog:", merchant_catalog())
    print("3. public catalog entries for our wallet:", public_catalog(), "(curated 500-slice)")
    print("4. onyx mirror mentions:", onyx_mirror())
    print("5. coinbase validate:", validate_index())
    print("\n'processing' = accepted, cataloging is async on Coinbase's side.")
    print("A settled payment already exists (tx 0x157a96b2…). Re-run this script later.")
