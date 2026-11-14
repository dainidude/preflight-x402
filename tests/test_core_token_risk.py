"""Tests for core/token_risk.py Greenlight cross-reference notes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import web3
from web3.providers.base import BaseProvider

from preflight.core import simulation as simulation_mod
from preflight.core import token_risk as token_risk_mod
from preflight.core.token_risk import run_token_risk


class FakeHTTPProvider(BaseProvider):
    """Offline RPC provider that only answers eth_getCode."""

    _code = "0x"

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        super().__init__()

    def make_request(self, method: str, params: Any) -> dict[str, Any]:
        resp: dict[str, Any] = {"jsonrpc": "2.0", "id": 0}
        if method == "eth_getCode":
            resp["result"] = self._code
        elif method == "eth_chainId":
            resp["result"] = "0x2105"
        else:
            resp["result"] = "0x"
        return resp


def _set_rpc_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import preflight.config

    def fake_config() -> Any:
        return SimpleNamespace(rpc_url="http://localhost:8545")

    monkeypatch.setattr(preflight.config, "config", fake_config)


def _set_fake_provider(monkeypatch: pytest.MonkeyPatch, code_hex: str) -> None:
    monkeypatch.setattr(FakeHTTPProvider, "_code", code_hex)
    monkeypatch.setattr(web3.Web3, "HTTPProvider", FakeHTTPProvider)
    monkeypatch.setattr(web3.providers.rpc, "HTTPProvider", FakeHTTPProvider)
    monkeypatch.setattr(web3, "HTTPProvider", FakeHTTPProvider)


def _greenlight_note_present(notes: list[str]) -> bool:
    return any("Greenlight" in n and "pair-check" in n for n in notes)


@pytest.mark.asyncio
class TestTokenRiskGreenlightNote:
    async def test_note_on_canonical_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_rpc_config(monkeypatch)
        report = await run_token_risk("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        assert report.verdict == "looks_ok"
        assert _greenlight_note_present(report.notes)

    async def test_note_on_review_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_rpc_config(monkeypatch)
        _set_fake_provider(monkeypatch, "0x6000")
        monkeypatch.setattr(token_risk_mod, "_verdict_from", lambda findings: ("review", 50.0))
        monkeypatch.setattr(simulation_mod, "_anvil_path", lambda: None)
        report = await run_token_risk("0xdAC17F958D2ee523a2206206994597C13D831ec7")
        assert report.verdict == "review"
        assert _greenlight_note_present(report.notes)

    async def test_no_note_on_block_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_rpc_config(monkeypatch)
        _set_fake_provider(monkeypatch, "0x6000")
        monkeypatch.setattr(token_risk_mod, "_verdict_from", lambda findings: ("block", 80.0))
        monkeypatch.setattr(simulation_mod, "_anvil_path", lambda: None)
        report = await run_token_risk("0xdAC17F958D2ee523a2206206994597C13D831ec7")
        assert report.verdict == "block"
        assert not _greenlight_note_present(report.notes)
