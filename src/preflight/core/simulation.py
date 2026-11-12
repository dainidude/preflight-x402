"""RPC-based transaction pre-flight simulation (quick + deep anvil fork)."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
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

DEFAULT_MAINNET_RPC = "https://mainnet.base.org"
DEFAULT_TESTNET_RPC = "https://sepolia.base.org"

ANVIL_START_TIMEOUT = 10.0
ANVIL_READY_TIMEOUT = 10.0
ANVIL_TX_TIMEOUT = 30.0
ANVIL_TOTAL_TIMEOUT = 90.0


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


def _deep_unavailable_finding(context: str) -> Finding:
    return Finding(
        id="sim:deep-unavailable",
        title="Deep simulation unavailable",
        severity="low",
        confidence=0.9,
        description=context,
        snippet=None,
        detector="heuristic:sim",
    )


def _create_web3(rpc_url: str) -> Web3:
    """Build a web3 instance for the given RPC URL."""
    return Web3(Web3.HTTPProvider(rpc_url))


def _anvil_path() -> str | None:
    """Locate the anvil binary, checking common foundry install paths."""
    import glob as _glob

    extra = [
        os.path.expanduser("~/.config/.foundry/bin"),
        os.path.expanduser("~/.foundry/bin"),
    ]
    # foundryup also installs into versioned dirs; add their parents to PATH search
    for pattern in (
        os.path.expanduser("~/.config/.foundry/versions/*/*/*/anvil"),
        os.path.expanduser("~/.foundry/versions/*/*/*/anvil"),
    ):
        for hit in sorted(_glob.glob(pattern)):
            if os.access(hit, os.X_OK):
                extra.insert(0, os.path.dirname(hit))
    env_path = os.environ.get("PATH", "")
    search_path = os.pathsep.join(extra + env_path.split(os.pathsep))
    return shutil.which("anvil", path=search_path) or shutil.which("anvil")


def _free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for_anvil(w3: Web3, timeout: float = ANVIL_READY_TIMEOUT) -> bool:
    """Poll the local anvil RPC until it responds or times out."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            w3.eth.chain_id
            return True
        except Exception:
            await asyncio.sleep(0.1)
    return False


async def _start_anvil(
    anvil_path: str, rpc_url: str, port: int
) -> asyncio.subprocess.Process:
    """Spawn a local anvil fork subprocess."""
    env = os.environ.copy()
    bin_dir = os.path.dirname(anvil_path)
    if bin_dir:
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

    return await asyncio.create_subprocess_exec(
        anvil_path,
        "--fork-url",
        rpc_url,
        "--port",
        str(port),
        "--no-rate-limit",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )


def _short_address(addr: str) -> str:
    """Shorten an address for display."""
    return addr[:8] if len(addr) >= 8 else addr


def _extract_internal_calls(trace: Any, out: list[str]) -> None:
    """Recursively collect CALL/DELEGATECALL/STATICCALL frames from a callTracer."""
    if len(out) >= 10:
        return
    if not isinstance(trace, dict):
        return

    typ = trace.get("type", "")
    if typ in ("CALL", "DELEGATECALL", "STATICCALL"):
        to = trace.get("to", "")
        inp = trace.get("input", "0x")
        sel = inp[2:10] if len(inp) >= 10 else inp[2:]
        if sel:
            out.append(f"{_short_address(to)} → 0x{sel}(...)")
        else:
            out.append(f"{_short_address(to)} → call")

    for child in trace.get("calls", []):
        _extract_internal_calls(child, out)
        if len(out) >= 10:
            return


def _build_sim_report(
    to: str,
    from_address: str,
    value_wei: str,
    reverted: bool,
    revert_reason: str | None,
    findings: list[Finding],
    notes: list[str],
    gas_used: str | None = None,
    balance_delta_wei: str | None = None,
    internal_calls: list[str] | None = None,
    simulator: str = "rpc-ethcall:v1",
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
        balance_delta_wei=balance_delta_wei,
        internal_calls=internal_calls or [],
        simulated_at=_utc_now(),
        simulator=simulator,
        attestation=None,
        notes=notes,
    )


def _prepare_simulation(
    req: SimRequest,
) -> tuple[str, str, int, str, list[str], list[Finding]] | SimReport:
    """Validate addresses and normalize inputs shared by quick/deep modes.

    Returns a tuple on success, or a fully built SimReport when validation fails.
    """
    notes: list[str] = []
    findings: list[Finding] = []

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
    return to_address, from_address, value, data, notes, findings


async def _run_quick_simulation(
    req: SimRequest,
    to_address: str,
    from_address: str,
    value: int,
    data: str,
    notes: list[str],
    findings: list[Finding],
) -> SimReport:
    """Quick eth_call simulation against the configured RPC."""
    rpc_url = config().rpc_url or DEFAULT_MAINNET_RPC
    w3 = _create_web3(rpc_url)

    # Code check.
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

    # eth_call with optional balance override for value transfers.
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

    # Optional static analysis of target source.
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


async def _run_deep_simulation(
    req: SimRequest,
    to_address: str,
    from_address: str,
    value: int,
    data: str,
) -> SimReport:
    """Deep anvil-fork simulation with gas, balance delta, and internal calls."""
    rpc_url = config().rpc_url or DEFAULT_MAINNET_RPC

    anvil_path = _anvil_path()
    if not anvil_path:
        note = "anvil unavailable, fell back to quick mode"
        quick = await _run_quick_simulation(
            req, to_address, from_address, value, data, [], []
        )
        return quick.model_copy(
            update={
                "simulator": "anvil-fork:v1",
                "notes": quick.notes + [note],
                "findings": quick.findings + [_deep_unavailable_finding(note)],
            }
        )

    proc: asyncio.subprocess.Process | None = None
    try:
        async with asyncio.timeout(ANVIL_TOTAL_TIMEOUT):
            port = _free_port()
            proc = await _start_anvil(anvil_path, rpc_url, port)

            w3 = _create_web3(f"http://127.0.0.1:{port}")
            if not await _wait_for_anvil(w3, timeout=ANVIL_READY_TIMEOUT):
                raise RuntimeError("anvil did not become ready")

            # Fund and impersonate the simulated sender.
            w3.provider.make_request(
                "anvil_setBalance", [from_address, hex(10**30)]
            )
            w3.provider.make_request("anvil_impersonateAccount", [from_address])

            balance_before = w3.eth.get_balance(from_address)
            tx: dict[str, Any] = {
                "from": from_address,
                "to": to_address,
                "data": data,
                "value": value,
            }

            findings: list[Finding] = []
            notes: list[str] = []

            # Code check on the fork.
            try:
                code_hex = (
                    w3.eth.get_code(to_address).hex() or "0x"
                )
            except Exception:
                code_hex = "0x"
            is_eoa = code_hex in ("0x", "0x0")
            if is_eoa:
                if data not in (None, "", "0x"):
                    findings.append(_call_to_eoa_finding())
                    notes.append(
                        f"Target {to_address} has no code; calldata sent to EOA"
                    )
                else:
                    notes.append(
                        f"Target {to_address} is an EOA; plain value transfer"
                    )

            # Revert probe via eth_call.
            reverted = False
            revert_reason: str | None = None
            try:
                w3.eth.call(tx, "latest")
            except ContractLogicError as exc:
                reverted = True
                raw = _extract_revert_data(exc)
                revert_reason = _decode_revert(raw) if raw else str(exc)
                findings.append(_revert_finding(revert_reason))
                notes.append(f"eth_call reverted: {revert_reason}")

            # Gas estimate (best-effort; falls back to receipt gas after send).
            gas_used: str | None = None
            try:
                gas_used = str(w3.eth.estimate_gas(tx))
            except Exception:
                pass

            # Mine the transaction to obtain real gas usage and balance delta.
            tx_hash = w3.eth.send_transaction(tx)
            receipt = w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=ANVIL_TX_TIMEOUT
            )
            if receipt.gasUsed:
                gas_used = str(receipt.gasUsed)
            if receipt.status == 0:
                reverted = True
                if revert_reason is None:
                    try:
                        w3.eth.call(tx, receipt.blockNumber)
                    except ContractLogicError as exc:
                        raw = _extract_revert_data(exc)
                        revert_reason = _decode_revert(raw) if raw else str(exc)
                        notes.append(f"Transaction reverted: {revert_reason}")

            balance_after = w3.eth.get_balance(from_address)
            balance_delta_wei = str(balance_after - balance_before)

            # Internal calls via callTracer.
            internal_calls: list[str] = []
            try:
                trace_resp = w3.provider.make_request(
                    "debug_traceTransaction",
                    [
                        tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash),
                        {"tracer": "callTracer"},
                    ],
                )
                trace_result = trace_resp.get("result")
                if isinstance(trace_result, dict):
                    _extract_internal_calls(trace_result, internal_calls)
                    internal_calls = internal_calls[:10]
            except Exception:
                pass

            # Optional static analysis of target source.
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
                gas_used=gas_used,
                balance_delta_wei=balance_delta_wei,
                internal_calls=internal_calls,
                simulator="anvil-fork:v1",
            )

    except Exception as exc:
        note = f"anvil unavailable, fell back to quick mode ({exc})"
        quick = await _run_quick_simulation(
            req, to_address, from_address, value, data, [], []
        )
        return quick.model_copy(
            update={
                "simulator": "anvil-fork:v1",
                "notes": quick.notes + [note],
                "findings": quick.findings + [_deep_unavailable_finding(note)],
            }
        )
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass
        elif proc is not None:
            try:
                await proc.wait()
            except Exception:
                pass


async def run_simulation(req: SimRequest) -> SimReport:
    """Run a pre-flight transaction simulation in quick or deep mode."""
    prepared = _prepare_simulation(req)
    if isinstance(prepared, SimReport):
        return prepared

    to_address, from_address, value, data, notes, findings = prepared

    if req.mode == "deep":
        return await _run_deep_simulation(
            req, to_address, from_address, value, data
        )

    return await _run_quick_simulation(
        req, to_address, from_address, value, data, notes, findings
    )
