"""RPC-based transaction pre-flight simulation."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from eth_abi import decode as eth_abi_decode
from web3 import Web3
from web3.exceptions import ContractLogicError

from preflight.config import config
from preflight.core.detectors import DetectorContext, run_all_custom_detectors
from preflight.core.scoring import calculate_score, determine_verdict
from preflight.types import Finding, SimReport, SimRequest

SIMULATOR_ADDRESS = Web3.to_checksum_address("0x" + "aa" * 20)

ERROR_SELECTOR = "0x08c379a0"
PANIC_SELECTOR = "0x4e487b71"


def _utc_now() -> str:
    """ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_value_wei(value_wei: str | None) -> int:
    """Parse a decimal wei string to int, defaulting to 0."""
    if value_wei is None or value_wei == "":
        return 0
    return int(value_wei)


def _decode_revert(data_hex: str) -> str:
    """Decode Error(string) and Panic(uint256) revert data."""
    data_hex = data_hex.removeprefix("0x")
    if len(data_hex) < 8:
        return "0x" + data_hex

    selector = data_hex[:8]
    body = data_hex[8:]

    try:
        if selector == ERROR_SELECTOR[2:]:
            (reason,) = eth_abi_decode(["string"], bytes.fromhex(body))
            return str(reason)
        if selector == PANIC_SELECTOR[2:]:
            (code,) = eth_abi_decode(["uint256"], bytes.fromhex(body))
            return f"Panic({code})"
    except Exception:
        pass

    return "0x" + data_hex


def _extract_revert_data(exc: Exception) -> str | None:
    """Best-effort extraction of raw revert data from a web3 exception."""
    data = getattr(exc, "data", None)
    if isinstance(data, str) and data.startswith("0x"):
        return data

    msg = str(exc)
    match = re.search(r"0x[0-9a-fA-F]{8,}", msg)
    if match:
        return match.group()

    return None


def _validation_finding(description: str) -> Finding:
    return Finding(
        id="heuristic:validation:checksum",
        title="Invalid address",
        severity="medium",
        confidence=0.95,
        description=description,
        snippet=None,
        detector="heuristic:validation",
    )


def _call_to_eoa_finding() -> Finding:
    return Finding(
        id="sim:call-to-eoa",
        title="Call data sent to EOA",
        severity="low",
        confidence=0.9,
        description="The target address has no bytecode but calldata was supplied",
        snippet=None,
        detector="heuristic:sim",
    )


def _revert_finding(reason: str) -> Finding:
    return Finding(
        id="sim:revert",
        title="Transaction simulation reverted",
        severity="high",
        confidence=0.95,
        description=f"eth_call reverted: {reason}",
        snippet=None,
        detector="heuristic:sim",
    )


def _rpc_failure_finding(context: str) -> Finding:
    return Finding(
        id="sim:rpc-failure",
        title="RPC simulation unavailable",
        severity="info",
        confidence=0.9,
        description=context,
        snippet=None,
        detector="heuristic:sim",
    )


def _create_web3(rpc_url: str) -> Web3:
    """Build a web3 instance for the given RPC URL."""
    return Web3(Web3.HTTPProvider(rpc_url))


def _build_sim_report(
    to: str,
    from_address: str,
    value_wei: str,
    reverted: bool,
    revert_reason: str | None,
    findings: list[Finding],
    notes: list[str],
    gas_used: str | None = None,
) -> SimReport:
    return SimReport(
        verdict=determine_verdict(findings),
        score=calculate_score(findings),
        findings=findings,
        to=to,
        from_address=from_address,
        value_wei=value_wei,
        reverted=reverted,
        revert_reason=revert_reason,
        gas_used=gas_used,
        simulated_at=_utc_now(),
        simulator="rpc-ethcall:v1",
        attestation=None,
        notes=notes,
    )


async def run_simulation(req: SimRequest) -> SimReport:
    """Run an RPC-based pre-flight transaction simulation.

    Uses eth_call against the configured RPC_URL (default Base Sepolia).
    Returns a structured SimReport; RPC failures are surfaced as info findings
    and notes rather than exceptions.
    """
    notes: list[str] = []
    findings: list[Finding] = []

    # 1. Validate target address.
    try:
        to_address = Web3.to_checksum_address(req.to)
    except Exception:
        desc = f"Invalid 'to' address: {req.to}"
        findings.append(_validation_finding(desc))
        notes.append(desc)
        return _build_sim_report(
            to=req.to,
            from_address=req.from_address or SIMULATOR_ADDRESS,
            value_wei=req.value_wei or "0",
            reverted=False,
            revert_reason=None,
            findings=findings,
            notes=notes,
        )

    # 2. Determine sender.
    from_address = SIMULATOR_ADDRESS
    if req.from_address:
        try:
            from_address = Web3.to_checksum_address(req.from_address)
        except Exception:
            note = (
                f"Invalid 'from_address' {req.from_address}; "
                f"using simulator address {SIMULATOR_ADDRESS}"
            )
            notes.append(note)

    value = _parse_value_wei(req.value_wei)
    data = req.data if req.data else "0x"

    # 3. RPC setup (lazily built per call so tests can monkeypatch _create_web3).
    rpc_url = config().rpc_url or "https://sepolia.base.org"
    w3 = _create_web3(rpc_url)

    # 4. Code check.
    code_hex = "0x"
    try:
        code = w3.eth.get_code(to_address)
        code_hex = (code.hex() if hasattr(code, "hex") else str(code)) or "0x"
    except Exception as exc:
        note = f"RPC call eth_getCode failed: {exc}"
        notes.append(note)
        findings.append(_rpc_failure_finding(note))

    is_eoa = code_hex in ("0x", "0x0")
    if is_eoa:
        if data not in (None, "", "0x"):
            findings.append(_call_to_eoa_finding())
            notes.append(f"Target {to_address} has no code; calldata sent to EOA")
        else:
            notes.append(f"Target {to_address} is an EOA; plain value transfer")

    # 5. eth_call with optional balance override for value transfers.
    state_override: dict[str, Any] | None = None
    if value > 0:
        state_override = {from_address: {"balance": hex(10**30)}}
        notes.append(
            "Balance override applied to simulated sender so value transfers "
            "do not fail due to insufficient funds"
        )

    reverted = False
    revert_reason: str | None = None
    try:
        tx = {"from": from_address, "to": to_address, "data": data, "value": value}
        if state_override is not None:
            w3.eth.call(tx, "latest", state_override)
        else:
            w3.eth.call(tx, "latest")
    except ContractLogicError as exc:
        reverted = True
        raw = _extract_revert_data(exc)
        revert_reason = _decode_revert(raw) if raw else str(exc)
        findings.append(_revert_finding(revert_reason))
        notes.append(f"eth_call reverted: {revert_reason}")
    except Exception as exc:
        note = f"RPC call eth_call failed: {exc}"
        notes.append(note)
        findings.append(_rpc_failure_finding(note))

    # 6. Optional static analysis of target source.
    if req.source:
        ctx = DetectorContext(
            source=req.source,
            lines=req.source.splitlines(),
            contract_name=None,
            solc_version=None,
        )
        findings.extend(run_all_custom_detectors(ctx))

    return _build_sim_report(
        to=to_address,
        from_address=from_address,
        value_wei=str(value),
        reverted=reverted,
        revert_reason=revert_reason,
        findings=findings,
        notes=notes,
    )
