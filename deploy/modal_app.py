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
    "PREFLIGHT_NETWORK": os.getenv("PREFLIGHT_NETWORK", "base-sepolia"),
    "PREFLIGHT_FACILITATOR": os.getenv("PREFLIGHT_FACILITATOR", "https://x402.org/facilitator"),
    "PREFLIGHT_PORT": "8000",
    "DEV_MODE": "0",
    "FREE_TIER": os.getenv("FREE_TIER", "1"),
    "PREFLIGHT_ATTEST_KEY": os.getenv("PREFLIGHT_ATTEST_KEY", ""),
    "RPC_URL": os.getenv("RPC_URL", "https://sepolia.base.org"),
    "DISCORD_WEBHOOK_URL": os.getenv("DISCORD_WEBHOOK_URL", ""),
}

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_pyproject(REPO / "pyproject.toml")
    # Pre-bake solc so the first scan is fast (py-solc-x downloads to ~/.solcx).
    .run_commands(
        "python -c \"import solcx; [solcx.install_solc(v) for v in ('0.8.20','0.8.24','0.8.26')]\"",
    )
    .add_local_dir(REPO / "src", "/app/src", copy=True)
    .add_local_dir(REPO / "public", "/app/public", copy=True)
    .add_local_file(REPO / "pyproject.toml", "/app/pyproject.toml")
    .workdir("/app")
    .env({"PYTHONPATH": "/app/src"})
    .run_commands("python -c \"import preflight.api.app as a; print('image import ok:', a.app.title)\"")
)

app = modal.App("preflight-api", image=image)


@app.function(
    secrets=[modal.Secret.from_dict(DEPLOY_ENV)],
    min_containers=1,  # always-on: agents must find us on the first request
    timeout=300,
)
@modal.web_server(8000, requires_proxy_auth=False)
def run_api() -> None:
    import uvicorn

    uvicorn.run("preflight.api.app:app", host="0.0.0.0", port=8000, log_level="info")
