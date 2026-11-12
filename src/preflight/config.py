"""Central configuration via environment variables. Arbiter-owned."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


# Canonical USDC addresses per network (mainnet-ready: flipping
# PREFLIGHT_NETWORK to "base" + a funded pay_to wallet is the whole switch).
NETWORK_USDC: dict[str, str] = {
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}


class Config:
    """Loaded lazily so tests can monkeypatch env before first use."""

    _instance: Config | None = None

    def __new__(cls) -> Config:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_loaded", False):
            return
        self._loaded = True
        self.pay_to_address: str | None = os.getenv("PREFLIGHT_PAY_TO_ADDRESS")
        self.network: str = os.getenv("PREFLIGHT_NETWORK", "base-sepolia")
        self.facilitator_url: str = os.getenv("PREFLIGHT_FACILITATOR", "https://x402.org/facilitator")
        self.port: int = int(os.getenv("PREFLIGHT_PORT", "8000"))
        self.dev_mode: bool = _bool("DEV_MODE", default=False)
        self.free_tier: bool = _bool("FREE_TIER", default=True)
        self.attest_key_hex: str | None = os.getenv("PREFLIGHT_ATTEST_KEY")
        self.api_url: str = os.getenv("PREFLIGHT_API_URL", f"http://127.0.0.1:{self.port}")
        self.rpc_url: str | None = os.getenv("RPC_URL")
        # USDC asset derived from network (override with PREFLIGHT_USDC_ASSET).
        self.usdc_asset: str = (
            os.getenv("PREFLIGHT_USDC_ASSET")
            or NETWORK_USDC.get(self.network, NETWORK_USDC["base-sepolia"])
        )
        self.client_private_key: str | None = os.getenv("CLIENT_PRIVATE_KEY")
        # Durable-state directory (Modal Volume in prod; empty = in-memory only)
        self.state_dir: str | None = os.getenv("STATE_DIR") or None
        # CDP Facilitator credentials (optional): when both are set, the server
        # verifies/settles via Coinbase's CDP Facilitator (KYT/OFAC + the Bazaar
        # discovery that indexes settled payments). Fallback: x402.org facilitator.
        self.cdp_api_key_id: str | None = os.getenv("CDP_API_KEY_ID")
        self.cdp_api_key_secret: str | None = os.getenv("CDP_API_KEY_SECRET")

    @classmethod
    def reload(cls) -> Config:
        cls._instance = None
        return cls()


def config() -> Config:
    return Config()
