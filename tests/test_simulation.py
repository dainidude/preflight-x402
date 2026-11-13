"""Tests for the RPC-based transaction simulation module."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from eth_abi import encode as eth_abi_encode
from web3 import Web3
from web3.exceptions import ContractLogicError

from preflight.core import simulation as simulation_mod
from preflight.core.simulation import (
    ERROR_SELECTOR,
    PANIC_SELECTOR,
    SIMULATOR_ADDRESS,
    TRANSFER_SELECTOR,
    _decode_revert,
    _probe_honeypot_transfer,
    run_simulation,
)
from preflight.types import SimRequest


class MockEth:
    """Mock web3.eth for offline simulation tests."""

    def __init__(
        self,
        code: str = "0x6000",
        revert_data: str | None = None,
        fail_call: bool = False,
        fail_code: bool = False,
    ):
        self.code = code
        self.revert_data = revert_data
        self.fail_call = fail_call
        self.fail_code = fail_code
        self.last_call_tx: dict[str, Any] | None = None
        self.last_state_override: dict[str, Any] | None = None

    def get_code(self, address: str) -> bytes:
        if self.fail_code:
            raise ConnectionError("RPC offline")
        return bytes.fromhex(self.code.removeprefix("0x"))

    def call(
        self,
        tx: dict[str, Any],
        block: str = "latest",
        state_override: dict[str, Any] | None = None,
    ) -> bytes:
        self.last_call_tx = tx
        self.last_state_override = state_override
        if self.fail_call:
            raise ConnectionError("RPC offline")
        if self.revert_data:
            raise ContractLogicError("execution reverted", data=self.revert_data)
        return b""


def _mock_w3_factory(code: str = "0x6000", **kwargs: Any):
    """Return a factory that replaces _create_web3 in simulation.py."""
    eth = MockEth(code=code, **kwargs)

    def factory(rpc_url: str) -> SimpleNamespace:
        return SimpleNamespace(eth=eth)

    factory._eth = eth  # expose for assertions
    return factory


class TestRevertDecoding:
    def test_decode_error_string(self):
        reason = "transfer rejected"
        data = ERROR_SELECTOR + eth_abi_encode(["string"], [reason]).hex()
        assert _decode_revert(data) == reason

    def test_decode_panic(self):
        data = PANIC_SELECTOR + eth_abi_encode(["uint256"], [17]).hex()
        assert _decode_revert(data) == "Panic(17)"

    def test_decode_unknown(self):
        data = "0xdeadbeef1234"
        assert _decode_revert(data) == data


class TestRunSimulation:
    @pytest.mark.asyncio
    async def test_invalid_to_address(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(simulation_mod, "_create_web3", _mock_w3_factory())
        # 42 chars but invalid checksum/hex.
        req = SimRequest(to="0x000000000000000000000000000000000000000g")
        report = await run_simulation(req)
        assert report.verdict == "review"
        assert any(f.id == "heuristic:validation:checksum" for f in report.findings)
        assert report.reverted is False

    @pytest.mark.asyncio
    async def test_call_to_contract_no_revert(self, monkeypatch: pytest.MonkeyPatch):
        mock = _mock_w3_factory(code="0x60006000")
        monkeypatch.setattr(simulation_mod, "_create_web3", mock)
        req = SimRequest(to="0xdAC17F958D2ee523a2206206994597C13D831ec7", data="0x")
        report = await run_simulation(req)
        assert report.verdict == "looks_ok"
        assert report.reverted is False
        assert report.from_address == SIMULATOR_ADDRESS

    @pytest.mark.asyncio
    async def test_call_to_eoa_with_data(self, monkeypatch: pytest.MonkeyPatch):
        mock = _mock_w3_factory(code="0x")
        monkeypatch.setattr(simulation_mod, "_create_web3", mock)
        req = SimRequest(
            to="0x0000000000000000000000000000000000000001", data="0x1234"
        )
        report = await run_simulation(req)
        assert report.verdict == "looks_ok"
        assert any(f.id == "sim:call-to-eoa" for f in report.findings)
        assert "Target" in " ".join(report.notes)

    @pytest.mark.asyncio
    async def test_plain_value_transfer_to_eoa(self, monkeypatch: pytest.MonkeyPatch):
        mock = _mock_w3_factory(code="0x")
        monkeypatch.setattr(simulation_mod, "_create_web3", mock)
        req = SimRequest(
            to="0x0000000000000000000000000000000000000001",
            value_wei="1000000000000000",
        )
        report = await run_simulation(req)
        assert report.verdict == "looks_ok"
        assert report.value_wei == "1000000000000000"
        assert mock._eth.last_state_override is not None
        assert "Balance override" in " ".join(report.notes)

    @pytest.mark.asyncio
    async def test_revert_error_string(self, monkeypatch: pytest.MonkeyPatch):
        reason = "insufficient allowance"
        data = ERROR_SELECTOR + eth_abi_encode(["string"], [reason]).hex()
        mock = _mock_w3_factory(code="0x6000", revert_data=data)
        monkeypatch.setattr(simulation_mod, "_create_web3", mock)
        req = SimRequest(
            to="0xdAC17F958D2ee523a2206206994597C13D831ec7", data="0x1234"
        )
        report = await run_simulation(req)
        assert report.reverted is True
        assert report.revert_reason == reason
        assert report.verdict == "block"
        assert any(f.id == "sim:revert" for f in report.findings)

    @pytest.mark.asyncio
    async def test_revert_panic(self, monkeypatch: pytest.MonkeyPatch):
        data = PANIC_SELECTOR + eth_abi_encode(["uint256"], [1]).hex()
        mock = _mock_w3_factory(code="0x6000", revert_data=data)
        monkeypatch.setattr(simulation_mod, "_create_web3", mock)
        req = SimRequest(
            to="0xdAC17F958D2ee523a2206206994597C13D831ec7", data="0x1234"
        )
        report = await run_simulation(req)
        assert report.reverted is True
        assert report.revert_reason == "Panic(1)"
        assert report.verdict == "block"

    @pytest.mark.asyncio
    async def test_rpc_failure_does_not_raise(self, monkeypatch: pytest.MonkeyPatch):
        mock = _mock_w3_factory(code="0x6000", fail_call=True, fail_code=True)
        monkeypatch.setattr(simulation_mod, "_create_web3", mock)
        req = SimRequest(
            to="0xdAC17F958D2ee523a2206206994597C13D831ec7", data="0x1234"
        )
        report = await run_simulation(req)
        # Grill fix: RPC failure must surface as review, never "looks_ok" —
        # a paying customer can't be told "safe" when we couldn't check.
        assert report.verdict == "review"
        assert any(
            f.id == "sim:rpc-failure" and f.severity == "medium" for f in report.findings
        )
        assert "RPC" in " ".join(report.notes)

    @pytest.mark.asyncio
    async def test_source_detectors_merged(self, monkeypatch: pytest.MonkeyPatch):
        mock = _mock_w3_factory(code="0x6000")
        monkeypatch.setattr(simulation_mod, "_create_web3", mock)
        source = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract BlacklistToken {
    mapping(address => bool) public isBlacklisted;
    address public owner;
    constructor() { owner = msg.sender; }
    function transfer(address to, uint256 amount) external {
        require(!isBlacklisted[to], "blacklisted");
    }
    function setBlacklist(address account, bool status) external {
        require(msg.sender == owner, "Not owner");
        isBlacklisted[account] = status;
    }
}
"""
        req = SimRequest(
            to="0xdAC17F958D2ee523a2206206994597C13D831ec7",
            data="0x1234",
            source=source,
        )
        report = await run_simulation(req)
        assert any("honeypot" in f.detector for f in report.findings)
        assert report.verdict == "block"

    @pytest.mark.asyncio
    async def test_custom_from_address(self, monkeypatch: pytest.MonkeyPatch):
        mock = _mock_w3_factory(code="0x6000")
        monkeypatch.setattr(simulation_mod, "_create_web3", mock)
        custom = Web3.to_checksum_address("0x" + "11" * 20)
        req = SimRequest(
            to="0xdAC17F958D2ee523a2206206994597C13D831ec7",
            data="0x",
            from_address=custom,
        )
        report = await run_simulation(req)
        assert report.from_address == custom


class TestDeepSimulation:
    @pytest.mark.asyncio
    async def test_deep_falls_back_when_anvil_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(simulation_mod, "_anvil_path", lambda: None)
        req = SimRequest(
            to="0xdAC17F958D2ee523a2206206994597C13D831ec7",
            data="0x1234",
            mode="deep",
        )
        report = await run_simulation(req)
        assert report.simulator == "anvil-fork:v1"
        assert any(f.id == "sim:deep-unavailable" for f in report.findings)
        assert "fell back to quick mode" in " ".join(report.notes)


def _mock_w3_for_probe(
    token: str,
    code: str,
    holder: str,
    holder_reverts: bool = False,
    contract_reverts: bool = False,
    setup_fail: bool = False,
    code_fail: bool = False,
):
    """Return a mock web3 and a log of provider calls for honeypot probe tests."""
    calls: list[tuple[str, Any]] = []

    class MockEth:
        def get_code(self, address: str) -> bytes:
            if code_fail:
                raise ConnectionError("RPC offline")
            return bytes.fromhex(code.removeprefix("0x"))

        def call(self, tx: dict[str, Any], block: str = "latest") -> bytes:
            calls.append(("eth_call", tx))
            if tx["from"].lower() == holder.lower():
                if holder_reverts:
                    raise ContractLogicError(
                        "execution reverted",
                        data=ERROR_SELECTOR
                        + eth_abi_encode(["string"], ["holder blocked"]).hex(),
                    )
                return b""
            if tx["from"].lower() == token.lower():
                if contract_reverts:
                    raise ContractLogicError(
                        "execution reverted",
                        data=ERROR_SELECTOR
                        + eth_abi_encode(["string"], ["contract blocked"]).hex(),
                    )
                return b""
            raise ContractLogicError("unexpected from address")

    class MockProvider:
        def make_request(self, method: str, params: list[Any]) -> dict[str, Any]:
            calls.append((method, params))
            if setup_fail:
                raise ConnectionError("anvil offline")
            return {}

    return SimpleNamespace(eth=MockEth(), provider=MockProvider()), calls


class TestHoneypotProbe:
    @pytest.mark.asyncio
    async def test_detects_holder_restricted(self, monkeypatch: pytest.MonkeyPatch):
        token = "0x" + "34" * 20
        holder = "0x" + "12" * 20
        monkeypatch.setattr(simulation_mod, "_random_address", lambda: holder)
        w3, calls = _mock_w3_for_probe(
            token, TRANSFER_SELECTOR[2:] + "00" * 100, holder,
            holder_reverts=True, contract_reverts=False,
        )
        findings, notes = _probe_honeypot_transfer(w3, token, SIMULATOR_ADDRESS)
        assert any(f.id == "honeypot:transfer-restricted" for f in findings)
        assert any("holder transfer reverted" in n for n in notes)
        # Impersonation setup calls.
        assert any(method == "anvil_impersonateAccount" for method, _ in calls)

    @pytest.mark.asyncio
    async def test_no_finding_when_both_succeed(self, monkeypatch: pytest.MonkeyPatch):
        token = "0x" + "34" * 20
        holder = "0x" + "12" * 20
        monkeypatch.setattr(simulation_mod, "_random_address", lambda: holder)
        w3, _ = _mock_w3_for_probe(
            token, TRANSFER_SELECTOR[2:] + "00" * 100, holder,
            holder_reverts=False, contract_reverts=False,
        )
        findings, notes = _probe_honeypot_transfer(w3, token, SIMULATOR_ADDRESS)
        assert not any(f.id == "honeypot:transfer-restricted" for f in findings)
        assert any("succeeded" in n for n in notes)

    @pytest.mark.asyncio
    async def test_no_finding_when_both_revert(self, monkeypatch: pytest.MonkeyPatch):
        token = "0x" + "34" * 20
        holder = "0x" + "12" * 20
        monkeypatch.setattr(simulation_mod, "_random_address", lambda: holder)
        w3, _ = _mock_w3_for_probe(
            token, TRANSFER_SELECTOR[2:] + "00" * 100, holder,
            holder_reverts=True, contract_reverts=True,
        )
        findings, notes = _probe_honeypot_transfer(w3, token, SIMULATOR_ADDRESS)
        assert not any(f.id == "honeypot:transfer-restricted" for f in findings)
        assert any("both holder and contract-self transfers reverted" in n for n in notes)

    @pytest.mark.asyncio
    async def test_skipped_when_not_erc20(self, monkeypatch: pytest.MonkeyPatch):
        token = "0x" + "34" * 20
        holder = "0x" + "12" * 20
        monkeypatch.setattr(simulation_mod, "_random_address", lambda: holder)
        w3, _ = _mock_w3_for_probe(token, "0x6000", holder)
        findings, notes = _probe_honeypot_transfer(w3, token, SIMULATOR_ADDRESS)
        assert not findings
        assert any("does not look like an ERC-20" in n for n in notes)

    @pytest.mark.asyncio
    async def test_skipped_on_setup_failure(self, monkeypatch: pytest.MonkeyPatch):
        token = "0x" + "34" * 20
        holder = "0x" + "12" * 20
        monkeypatch.setattr(simulation_mod, "_random_address", lambda: holder)
        w3, _ = _mock_w3_for_probe(
            token, TRANSFER_SELECTOR[2:] + "00" * 100, holder, setup_fail=True,
        )
        findings, notes = _probe_honeypot_transfer(w3, token, SIMULATOR_ADDRESS)
        assert not findings
        assert any("anvil setup failed" in n for n in notes)


class TestAnvilLifecycle:
    @pytest.mark.asyncio
    async def test_anvil_process_terminated_when_ready_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """If anvil spawns but never becomes ready, the subprocess must still die."""

        class FakeProc:
            def __init__(self) -> None:
                self.returncode: int | None = None
                self.terminated = False

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = 0

            async def wait(self) -> int:
                return 0

        fake = FakeProc()
        monkeypatch.setattr(simulation_mod, "_anvil_path", lambda: "/fake/anvil")

        async def fake_start(*_a: Any, **_kw: Any) -> FakeProc:
            return fake

        async def fake_wait(*_a: Any, **_kw: Any) -> bool:
            return False

        monkeypatch.setattr(simulation_mod, "_start_anvil", fake_start)
        monkeypatch.setattr(simulation_mod, "_wait_for_anvil", fake_wait)
        monkeypatch.setattr(simulation_mod, "_create_web3", _mock_w3_factory())

        req = SimRequest(
            to="0xdAC17F958D2ee523a2206206994597C13D831ec7",
            data="0x",
            mode="deep",
        )
        report = await run_simulation(req)
        assert fake.terminated, "anvil subprocess was not cleaned up"
        assert any(f.id == "sim:deep-unavailable" for f in report.findings)


class TestRunSimulationNetwork:
    @pytest.mark.network
    @pytest.mark.asyncio
    async def test_live_erc20_call(self):
        req = SimRequest(
            to="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # Base mainnet USDC
            data="0x70a082310000000000000000000000000000000000000000000000000000000000000000",
        )
        report = await run_simulation(req)
        assert report.reverted is False
        assert report.simulator == "rpc-ethcall:v1"

    @pytest.mark.network
    @pytest.mark.asyncio
    async def test_live_deep_usdc_balance_call(self):
        req = SimRequest(
            to="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # Base mainnet USDC
            data="0x70a082310000000000000000000000000000000000000000000000000000000000000000",
            mode="deep",
        )
        report = await run_simulation(req)
        assert report.simulator == "anvil-fork:v1"
        assert report.reverted is False
        assert report.gas_used is not None
        assert report.balance_delta_wei is not None
        assert int(report.balance_delta_wei) <= 0  # sender paid gas

    @pytest.mark.network
    @pytest.mark.asyncio
    async def test_live_deep_usdc_honeypot_probe(self):
        req = SimRequest(
            to="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # Base mainnet USDC
            data="0x",
            mode="deep",
        )
        report = await run_simulation(req)
        assert report.simulator == "anvil-fork:v1"
        assert not any(
            f.id == "honeypot:transfer-restricted" for f in report.findings
        ), report.notes
