"""Preflight on Modal — permanent public URL deployment.

Deploy (from repo root, with .env loaded):
    set -a && source .env && set +a && modal deploy deploy/modal_app.py

Result: https://<workspace>--preflight-api--8000.modal.run
       (Modal prints the exact URL on deploy)

Env/secret values are read from the local environment at deploy time and
injected as a Modal Secret — they never live in the repo.
"""

from __future__ import annotations

import os
from pathlib import Path

import modal

REPO = Path(__file__).resolve().parent.parent

# Values injected into the deployed container at deploy time.
DEPLOY_ENV = {
    "PREFLIGHT_PAY_TO_ADDRESS": os.getenv("PREFLIGHT_PAY_TO_ADDRESS", ""),
    "PREFLIGHT_NETWORK": os.getenv("PREFLIGHT_NETWORK", "base"),
    "PREFLIGHT_FACILITATOR": os.getenv("PREFLIGHT_FACILITATOR", "https://x402.org/facilitator"),
    "PREFLIGHT_PORT": "8000",
    "DEV_MODE": "0",
    "FREE_TIER": os.getenv("FREE_TIER", "1"),
    "PREFLIGHT_ATTEST_KEY": os.getenv("PREFLIGHT_ATTEST_KEY", ""),
    "RPC_URL": os.getenv("RPC_URL", "https://mainnet.base.org"),
    "DISCORD_WEBHOOK_URL": os.getenv("DISCORD_WEBHOOK_URL", ""),
    # CDP Facilitator (set ⇒ verify/settle via Coinbase KYT/OFAC + Bazaar indexing)
    "CDP_API_KEY_ID": os.getenv("CDP_API_KEY_ID", ""),
    "CDP_API_KEY_SECRET": os.getenv("CDP_API_KEY_SECRET", ""),
    # Durable state on a Modal Volume: metrics/quotas survive redeploys
    "STATE_DIR": "/data/state",
    "PREFLIGHT_API_URL": "https://preflight--preflight-api-api.us-east.modal.run",
}

_state_volume = modal.Volume.from_name("preflight-state", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_pyproject(REPO / "pyproject.toml")
    .workdir("/app")
    .env({"PYTHONPATH": "/app/src"})
    # Pre-bake solc so the first scan is fast (py-solc-x downloads to ~/.solcx).
    .run_commands(
        "python -c \"import solcx; [solcx.install_solc(v) for v in ('0.8.20','0.8.24','0.8.26')]\"",
    )
    # Bake anvil so the deep simulation tier works in-container.
    .run_commands(
        "python -c \"import urllib.request; urllib.request.urlretrieve('https://github.com/foundry-rs/foundry/releases/download/v1.9.0/foundry_v1.9.0_linux_amd64.tar.gz', '/tmp/foundry.tar.gz')\"",
        "tar -xzf /tmp/foundry.tar.gz -C /usr/local/bin anvil && anvil --version",
    )
    # No copy=True: files are mounted fresh at container start, so every
    # deploy serves the latest source and public/ without image rebuilds.
    # (add_local_* must be LAST — nothing after them counts as a build step.)
    .add_local_dir(REPO / "src", "/app/src")
    .add_local_dir(REPO / "public", "/app/public")
)

app = modal.App("preflight-api", image=image)

# Deploy-time guard: mainnet must go through the CDP facilitator (KYT/OFAC +
# Bazaar indexing). A silently-empty key (e.g. .env missing locally) would
# otherwise fall back to the public testnet facilitator ON MAINNET.
if DEPLOY_ENV["PREFLIGHT_NETWORK"] == "base" and not (
    DEPLOY_ENV["CDP_API_KEY_ID"] and DEPLOY_ENV["CDP_API_KEY_SECRET"]
):
    raise SystemExit(
        "Refusing to deploy to mainnet without CDP_API_KEY_ID/CDP_API_KEY_SECRET "
        "in the environment. Source .env before deploying."
    )


@app.function(
    secrets=[modal.Secret.from_dict(DEPLOY_ENV)],
    volumes={"/data": _state_volume},
    min_containers=1,  # always-on: agents must find us on the first request
    timeout=300,
)
@modal.asgi_app()
def api():
    """Modal serves the FastAPI ASGI app directly — no uvicorn, no port binding."""
    from preflight.api.app import app

    return app


@app.function(
    image=image,
    secrets=[modal.Secret.from_dict(DEPLOY_ENV)],
    min_containers=1,  # registry/Smithery users need a warm MCP endpoint
    timeout=600,
)
@modal.asgi_app()
def mcp():
    """Hosted MCP server: streamable HTTP at /mcp + Smithery server card.

    URL: https://preflight--preflight-api-mcp.modal.run/mcp
    """
    from preflight.mcp.server import create_http_app

    return create_http_app()


def _extract_verdicts(reports: list) -> list[dict]:
    """Convert ScanReports to the serializable verdict dicts for /verdicts."""
    import re as _re

    out: list[dict] = []
    for report in reports:
        symbol, address = "UNKNOWN", ""
        for note in report.notes:
            if note.startswith("Token:"):
                m = _re.search(r"Token:\s+(\S+)\s+\((0x[a-fA-F0-9]+)\)", note)
                if m:
                    symbol = m.group(1)
                    address = m.group(2)
                break
        out.append({
            "symbol": symbol,
            "address": address,
            "verdict": report.verdict,
            "score": report.score,
            "expires_at": report.expires_at,
            "top_finding": report.findings[0].title if report.findings else None,
            "scanned_at": report.scanned_at,
        })
    return out


def _save_verdicts(verdicts: list[dict]) -> None:
    """Append to /data/state/verdicts.json (dedupe by address+timestamp, keep ~90)."""
    import json as _json
    from pathlib import Path

    vf = Path("/data/state/verdicts.json")
    vf.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    try:
        existing = _json.loads(vf.read_text())
    except Exception:
        pass
    seen: set[str] = set()
    deduped: list[dict] = []
    for v in verdicts + existing:
        key = f"{v['address']}:{v['scanned_at']}"
        if key not in seen:
            seen.add(key)
            deduped.append(v)
    vf.write_text(_json.dumps(deduped[:90], indent=2))


@app.function(
    image=image,
    secrets=[modal.Secret.from_dict(DEPLOY_ENV)],
    volumes={"/data": _state_volume},
    schedule=modal.Cron("0 9 * * *"),
    timeout=300,
)
def daily_digest() -> None:
    """09:00 UTC daily: funnel summary + trending-token verdicts to Discord."""
    import asyncio
    from pathlib import Path

    import httpx

    from preflight.api.state import format_digest
    from preflight.core.trending import format_trending_cards, scan_trending_tokens

    api_url = os.environ.get("PREFLIGHT_API_URL", "").rstrip("/")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not (api_url and webhook):
        print("digest: PREFLIGHT_API_URL or DISCORD_WEBHOOK_URL missing — skipping")
        return
    text = ""
    try:
        metrics = httpx.get(f"{api_url}/metrics", timeout=30.0).raise_for_status().json()
        text = format_digest(metrics.get("metrics", {}))
    except Exception as exc:
        text = f"Digest metrics fetch failed: {exc}"

    try:
        trending_reports = asyncio.run(scan_trending_tokens(3))
        _save_verdicts(_extract_verdicts(trending_reports))
        cards = format_trending_cards(trending_reports)
        if cards:
            text += "\n\n" + cards
    except Exception as exc:
        text += f"\n\n📈 Trending scan skipped: {exc}"

    try:
        httpx.post(webhook, json={"content": f"```\n{text}\n```"}, timeout=15.0)
    except Exception as exc:
        try:
            httpx.post(webhook, json={"content": f"Preflight daily digest failed: {exc}"}, timeout=15.0)
        except Exception:
            pass


@app.function(
    image=image,
    secrets=[modal.Secret.from_dict(DEPLOY_ENV)],
    volumes={"/data": _state_volume},
    schedule=modal.Cron("10 9 * * *"),  # 10 min after the digest — verdicts are fresh
    timeout=120,
)
def post_verdicts_to_x() -> None:
    """Post each trending verdict to X (Preflight account)."""
    import asyncio
    from pathlib import Path

    from preflight.notify.x_post import format_verdict_tweet, post_tweet

    verdicts_file = Path("/data/state/verdicts.json")
    if not verdicts_file.exists():
        print("no verdicts stored yet — skipping")
        return

    import json
    verdicts = json.loads(verdicts_file.read_text())
    today = verdicts[:3]  # the most recent scan
    for v in today:
        tweet = format_verdict_tweet(
            symbol=v["symbol"],
            verdict=v["verdict"],
            top_finding=v.get("top_finding") or "no findings",
            address=v["address"],
        )
        tweet_id = asyncio.run(post_tweet(tweet))
        if tweet_id:
            print(f"posted: {v['symbol']} → {tweet_id}")
        else:
            print(f"failed: {v['symbol']}")


@app.function(
    image=image,
    secrets=[modal.Secret.from_dict(DEPLOY_ENV)],
    volumes={"/data": _state_volume},
    schedule=modal.Cron("*/5 * * * *"),
    timeout=60,
)
def heartbeat() -> None:
    """Every 5 min: /health + 402 challenge probe; alert after 2 consecutive fails."""
    import json
    from pathlib import Path

    import httpx

    api_url = os.environ.get("PREFLIGHT_API_URL", "").rstrip("/")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not api_url:
        return

    ok = False
    try:
        health = httpx.get(f"{api_url}/health", timeout=20.0)
        challenge = httpx.post(
            f"{api_url}/v1/scan",
            json={"source": "contract A {}"},
            headers={"Content-Type": "application/json"},
            timeout=20.0,
        )
        ok = health.status_code == 200 and challenge.status_code == 402
    except Exception:
        ok = False

    streak_file = Path("/data/state/heartbeat.json")
    streak_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        streak = int(json.loads(streak_file.read_text()).get("fail_streak", 0))
    except Exception:
        streak = 0
    streak = 0 if ok else streak + 1
    try:
        streak_file.write_text(json.dumps({"fail_streak": streak}))
    except OSError:
        pass

    if streak == 2 and webhook:  # alert exactly once per outage
        try:
            httpx.post(
                webhook,
                json={"content": f"🚨 Preflight DOWN: health/402 probe failing ({api_url})"},
                timeout=15.0,
            )
        except Exception:
            pass
