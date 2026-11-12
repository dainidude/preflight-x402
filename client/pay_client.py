"""Preflight paying-agent demo client.

Walks the full x402 payment flow against any paid route:

    request -> 402 -> build signed payment -> retry -> verdict

Routes (``--route``):

* ``scan`` (default) — ``POST /v1/scan`` ($0.01), body ``{source, contract_name?}``
* ``sim`` — ``POST /v1/sim`` ($0.05), body ``{to, data?, value_wei?}``
* ``project`` — ``POST /v1/scan/project`` ($0.10), body ``{files, contract_name?}``
* ``address-risk`` — ``POST /v1/address-risk`` ($0.005), body ``{address}``

With ``--route sim`` and no ``--to`` it runs a zero-argument demo: a 0-value
``transfer(address,uint256)`` call to the test-USDC contract for an amount the
burner cannot hold. The simulated call reverts, which is a correct
``sim:revert`` finding — printed as such with exit code 0.

In ``--dev`` mode it relies on the server's ``DEV_MODE`` (no wallet), which is
the fastest way to smoke-test the stack. In normal mode it loads or generates a
burner keypair, checks the Base Sepolia test-USDC balance, and pays the 402.

Usage:

    uv run python client/pay_client.py --dev
    uv run python client/pay_client.py --route scan --file contract.sol
    uv run python client/pay_client.py --route sim --to 0x... --data 0x...
    uv run python client/pay_client.py --route sim          # revert demo
    uv run python client/pay_client.py --route project --file A.sol --file B.sol
    uv run python client/pay_client.py --route address-risk --address 0x...
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from web3 import Web3

from x402 import x402ClientSync
from x402.http.constants import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    X_PAYMENT_HEADER,
    X_PAYMENT_RESPONSE_HEADER,
)
from x402.http.utils import (
    decode_payment_response_header,
    encode_payment_signature_header,
    safe_base64_decode,
)
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import PaymentPayload, PaymentRequired, PaymentRequirements

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
DEFAULT_RPC_URL = "https://sepolia.base.org"
USDC_ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_DECIMALS = 6
DEFAULT_SIM_RECIPIENT = "0x3779bb827AB1F32A317fD30157Ac03Dc5CCA88aa"
# Demo transfer amount: 1e18 base units (1e12 USDC) — far more than a burner
# holds, so the simulated transfer() reverts with "insufficient balance".
DEMO_TRANSFER_AMOUNT = 10**18

_DEV_HEADER = "X-Preflight-Dev"
_DEV_HEADER_VALUE = "bypass"

_USDC_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

SAMPLE_SOURCE = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// Reentrancy-vulnerable vault: withdraw sends ETH before updating state.
contract VulnerableVault {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    function withdraw(uint256 amount) external {
        require(balances[msg.sender] >= amount, "insufficient balance");
        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "transfer failed");
        balances[msg.sender] -= amount;
    }
}
"""


@dataclass(frozen=True)
class Route:
    """A paid API route and its advertised price."""

    name: str
    path: str
    price: str
    amount_base_units: str


ROUTES: dict[str, Route] = {
    "scan": Route("scan", "/v1/scan", "$0.01", "10000"),
    "sim": Route("sim", "/v1/sim", "$0.05", "50000"),
    "project": Route("project", "/v1/scan/project", "$0.10", "100000"),
    "address-risk": Route("address-risk", "/v1/address-risk", "$0.005", "5000"),
}
DEFAULT_ROUTE = "scan"


# ---------------------------------------------------------------------------
# .env handling (no python-dotenv dependency; parse KEY=VALUE manually)
# ---------------------------------------------------------------------------


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Parse a simple ``KEY=VALUE`` file into a dict. Returns {} when absent."""
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def save_env(path: Path, values: dict[str, str]) -> None:
    """Merge ``values`` into the env file, preserving existing lines."""
    merged = load_env(path)
    merged.update(values)
    path.write_text("".join(f"{key}={value}\n" for key, value in merged.items()), encoding="utf-8")


def load_or_create_key(env_file: Path = ENV_FILE) -> tuple[str, bool]:
    """Return the burner private key hex, creating and persisting one if absent.

    Returns:
        ``(private_key_hex, created)`` where ``created`` is True when a new key
        was generated and written to ``.env``.
    """
    existing = load_env(env_file).get("CLIENT_PRIVATE_KEY")
    if existing:
        return existing, False
    account = Account.create()
    key_hex = account.key.hex()
    save_env(env_file, {"CLIENT_PRIVATE_KEY": key_hex})
    return key_hex, True


# ---------------------------------------------------------------------------
# On-chain helpers (network only; patched in unit tests)
# ---------------------------------------------------------------------------


def get_usdc_balance(address: str, rpc_url: str, asset: str = USDC_ASSET) -> int:
    """Return the USDC balance of ``address`` in smallest units (6 decimals)."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    contract = w3.eth.contract(address=Web3.to_checksum_address(asset), abi=_USDC_ABI)
    return int(contract.functions.balanceOf(Web3.to_checksum_address(address)).call())


# ---------------------------------------------------------------------------
# Route payloads (offline, pure)
# ---------------------------------------------------------------------------


def erc20_transfer_calldata(recipient: str, amount: int) -> str:
    """ABI-encode ``transfer(address,uint256)`` as 0x-hex calldata."""
    if not recipient.startswith("0x") or len(recipient) != 42:
        raise ValueError(f"invalid recipient address: {recipient!r}")
    if amount < 0:
        raise ValueError("transfer amount must be non-negative")
    selector = "a9059cbb"
    padded_recipient = recipient[2:].lower().rjust(64, "0")
    padded_amount = format(amount, "064x")
    return f"0x{selector}{padded_recipient}{padded_amount}"


def scan_payload(source: str, contract_name: str | None = None) -> dict[str, Any]:
    """Body for ``POST /v1/scan``."""
    payload: dict[str, Any] = {"source": source}
    if contract_name:
        payload["contract_name"] = contract_name
    return payload


def project_payload(files: dict[str, str], contract_name: str | None = None) -> dict[str, Any]:
    """Body for ``POST /v1/scan/project``."""
    if not files:
        raise ValueError("project route requires at least one file")
    payload: dict[str, Any] = {"files": files}
    if contract_name:
        payload["contract_name"] = contract_name
    return payload


def sim_payload(
    to: str | None = None,
    data: str | None = None,
    value_wei: str | None = None,
    from_address: str | None = None,
) -> dict[str, Any]:
    """Body for ``POST /v1/sim``.

    With no ``to``, builds the zero-argument demo: a 0-value ERC-20
    ``transfer`` to the test-USDC contract for an unaffordable amount, which
    the simulator will report as a revert (a valid ``sim:revert`` finding).
    """
    if to is None:
        return {
            "to": USDC_ASSET,
            "data": erc20_transfer_calldata(DEFAULT_SIM_RECIPIENT, DEMO_TRANSFER_AMOUNT),
            "value_wei": "0",
        }
    if not to.startswith("0x") or len(to) != 42:
        raise ValueError(f"--to must be a 0x-prefixed 20-byte address: {to!r}")
    payload: dict[str, Any] = {"to": to}
    if data is not None:
        if not data.startswith("0x"):
            raise ValueError("--data must be 0x-prefixed hex")
        payload["data"] = data
    if value_wei is not None:
        payload["value_wei"] = str(value_wei)
    if from_address is not None:
        payload["from_address"] = from_address
    return payload


def address_risk_payload(address: str) -> dict[str, Any]:
    """Body for ``POST /v1/address-risk``."""
    if not address.startswith("0x") or len(address) != 42:
        raise ValueError(f"--address must be a 0x-prefixed 20-byte address: {address!r}")
    return {"address": address}


def build_payload(
    route: str,
    *,
    source: str | None = None,
    contract_name: str | None = None,
    files: dict[str, str] | None = None,
    to: str | None = None,
    data: str | None = None,
    value_wei: str | None = None,
    from_address: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    """Build the JSON body for ``route`` from CLI-style inputs."""
    if route not in ROUTES:
        raise ValueError(f"unknown route {route!r}; choose from {', '.join(ROUTES)}")
    if route == "scan":
        return scan_payload(source or SAMPLE_SOURCE, contract_name)
    if route == "project":
        return project_payload(files or {"VulnerableVault.sol": SAMPLE_SOURCE}, contract_name)
    if route == "sim":
        return sim_payload(to, data, value_wei, from_address)
    if address is None:
        raise ValueError("--route address-risk requires --address")
    return address_risk_payload(address)


# ---------------------------------------------------------------------------
# Payment construction (offline; uses x402.client)
# ---------------------------------------------------------------------------


def build_x402_client(private_key_hex: str) -> x402ClientSync:
    """Build an x402 client with an exact-EVM scheme registered for the key."""
    account = Account.from_key(private_key_hex)
    signer = EthAccountSigner(account)
    client = x402ClientSync()
    register_exact_evm_client(client, signer)
    return client


def create_payment(payment_required: PaymentRequired, private_key_hex: str) -> PaymentPayload:
    """Build the signed payment payload for a parsed 402 response."""
    client = build_x402_client(private_key_hex)
    return client.create_payment_payload(payment_required)  # type: ignore[return-value]


def payment_headers(payment_payload: PaymentPayload) -> dict[str, str]:
    """Encode a payment payload into the correct x402 retry header."""
    header_name = PAYMENT_SIGNATURE_HEADER if payment_payload.x402_version == 2 else X_PAYMENT_HEADER
    return {header_name: encode_payment_signature_header(payment_payload)}


def _headers_dict(headers: httpx.Headers) -> dict[str, str]:
    return {key.upper(): value for key, value in headers.items()}


def parse_payment_required(headers: dict[str, str], body: bytes) -> PaymentRequired:
    """Decode a 402 response into a V2 ``PaymentRequired``.

    Tolerates the wire shapes x402 servers emit: a ``PAYMENT-REQUIRED`` header
    carrying a full ``PaymentRequired`` object OR a bare array of accepts (the
    Preflight API sends the bare array), and a V1-shaped JSON body with an
    ``accepts`` list. ``accepts`` items use camelCase aliases, so each is
    coerced through :class:`~x402.schemas.PaymentRequirements`.
    """
    header = headers.get(PAYMENT_REQUIRED_HEADER) or headers.get(X_PAYMENT_HEADER)
    if header:
        data = json.loads(safe_base64_decode(header))
    elif body:
        data = json.loads(body.decode("utf-8"))
    else:
        raise ValueError("402 response carried no x402 payment requirement")

    accepts = data if isinstance(data, list) else data.get("accepts", [])
    requirements = [PaymentRequirements.model_validate(item) for item in accepts]
    error = None if isinstance(data, list) else data.get("error")
    return PaymentRequired(x402_version=2, error=error, accepts=requirements)


def parse_settle_response(headers: dict[str, str]) -> dict[str, Any] | None:
    """Decode the settlement metadata (payment tx) from a paid response, if any."""
    value = headers.get(PAYMENT_RESPONSE_HEADER) or headers.get(X_PAYMENT_RESPONSE_HEADER)
    if not value:
        return None
    return decode_payment_response_header(value).model_dump(mode="json")


# ---------------------------------------------------------------------------
# HTTP exchange
# ---------------------------------------------------------------------------


def paid_exchange(
    url: str,
    payload: dict[str, Any],
    private_key_hex: str,
    http_client: httpx.Client,
) -> httpx.Response:
    """Run 402 -> sign -> retry on ``url`` and return the final response.

    Raises:
        RuntimeError: When the retry is still a 402 (payment rejected).
    """
    first = http_client.post(url, json=payload)
    if first.status_code != 402:
        first.raise_for_status()
        return first

    payment_required = parse_payment_required(_headers_dict(first.headers), first.content)
    signed = create_payment(payment_required, private_key_hex)
    retry = http_client.post(url, json=payload, headers=payment_headers(signed))
    if retry.status_code == 402:
        raise RuntimeError(f"payment rejected by {url}: {retry.text[:300]}")
    retry.raise_for_status()
    return retry


def post_paid(
    api_url: str,
    path: str,
    payload: dict[str, Any],
    private_key_hex: str,
    http_client: httpx.Client | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run the full x402 flow for ``path`` and return ``(report, settle)``.

    ``http_client`` is injectable for tests (e.g. ``httpx.Client(transport=...)``).
    """
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=120.0)
    url = f"{api_url.rstrip('/')}{path}"
    try:
        response = paid_exchange(url, payload, private_key_hex, client)
        return response.json(), parse_settle_response(_headers_dict(response.headers))
    finally:
        if owns_client:
            client.close()


def scan_paid(
    api_url: str,
    source: str,
    contract_name: str | None,
    private_key_hex: str,
    http_client: httpx.Client | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run the full x402 flow against ``/v1/scan`` (backwards-compatible wrapper)."""
    return post_paid(
        api_url,
        ROUTES["scan"].path,
        scan_payload(source, contract_name),
        private_key_hex,
        http_client=http_client,
    )


def post_dev(
    api_url: str,
    path: str,
    payload: dict[str, Any],
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """POST ``path`` in dev mode (server-side DEV_MODE bypass)."""
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=120.0)
    url = f"{api_url.rstrip('/')}{path}"
    try:
        response = client.post(url, json=payload, headers={_DEV_HEADER: _DEV_HEADER_VALUE})
        if response.status_code == 402:
            raise RuntimeError(
                "Server returned 402 in dev mode. Start the API with DEV_MODE=1 "
                "so payment is bypassed."
            )
        response.raise_for_status()
        return response.json()
    finally:
        if owns_client:
            client.close()


def scan_dev(
    api_url: str,
    source: str,
    contract_name: str | None,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """POST ``/v1/scan`` in dev mode (backwards-compatible wrapper)."""
    return post_dev(
        api_url,
        ROUTES["scan"].path,
        scan_payload(source, contract_name),
        http_client=http_client,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def print_report(report: dict[str, Any], settle: dict[str, Any] | None) -> None:
    """Pretty-print the verdict, findings, attestation, and payment tx."""
    print("=" * 64)
    print("PREFLIGHT SCAN RESULT")
    print("=" * 64)
    print(f"verdict:        {report.get('verdict')}")
    print(f"score:          {report.get('score')}/100")
    print(f"contract:       {report.get('contract_name')}")
    print(f"scanned_at:     {report.get('scanned_at')}")
    print(f"source_sha256:  {report.get('source_sha256')}")

    findings = report.get("findings", [])
    print(f"findings:       {len(findings)}")
    for finding in findings:
        print(f"  - [{finding.get('severity')}] {finding.get('title')} "
              f"(confidence={finding.get('confidence')}) [{finding.get('detector')}]")

    attestation = report.get("attestation")
    if attestation:
        print(f"attestation:    signer={attestation.get('signer')} "
              f"scheme={attestation.get('scheme')}")
    else:
        print("attestation:    none")

    _print_settlement(settle)


def print_sim_report(report: dict[str, Any], settle: dict[str, Any] | None) -> None:
    """Pretty-print a SimReport, including the revert as a first-class finding."""
    print("=" * 64)
    print("PREFLIGHT SIMULATION RESULT")
    print("=" * 64)
    print(f"verdict:        {report.get('verdict')}")
    print(f"score:          {report.get('score')}/100")
    print(f"to:             {report.get('to')}")
    print(f"from:           {report.get('from_address')}")
    print(f"value_wei:      {report.get('value_wei')}")
    print(f"reverted:       {report.get('reverted')}")
    print(f"revert_reason:  {report.get('revert_reason')}")
    print(f"gas_used:       {report.get('gas_used')}")
    print(f"simulated_at:   {report.get('simulated_at')}")
    print(f"simulator:      {report.get('simulator')}")

    findings = report.get("findings", [])
    print(f"findings:       {len(findings)}")
    for finding in findings:
        print(f"  - [{finding.get('severity')}] {finding.get('title')} "
              f"(confidence={finding.get('confidence')}) [{finding.get('detector')}]")
    if report.get("reverted"):
        print("note:           sim:revert is a correct finding, not a client error "
              "(exit 0)")

    attestation = report.get("attestation")
    if attestation:
        print(f"attestation:    signer={attestation.get('signer')} "
              f"scheme={attestation.get('scheme')}")
    else:
        print("attestation:    none")

    _print_settlement(settle)


def _print_settlement(settle: dict[str, Any] | None) -> None:
    if settle:
        print("-" * 64)
        print("PAYMENT")
        print(f"  success:     {settle.get('success')}")
        print(f"  transaction: {settle.get('transaction')}")
        print(f"  network:     {settle.get('network')}")
        print(f"  payer:       {settle.get('payer')}")


def print_result(route: str, report: dict[str, Any], settle: dict[str, Any] | None) -> None:
    """Render the right report shape for ``route``."""
    if route == "sim" or "reverted" in report:
        print_sim_report(report, settle)
    else:
        print_report(report, settle)


def _print_faucet_instructions(address: str) -> None:
    print("USDC balance is 0 on Base Sepolia.")
    print(f"Fund this burner address with test USDC ({USDC_ASSET}):")
    print(f"  {address}")
    print("Faucets:")
    print("  - Circle USDC faucet: https://faucet.circle.com/ (select Base Sepolia, USDC)")
    print("  - x402 testnet faucet (if available): https://x402.org")
    print("After funding, re-run:")
    print("  uv run python client/pay_client.py")
    print("Or skip the wallet entirely:")
    print("  uv run python client/pay_client.py --dev   (requires server DEV_MODE=1)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight paying-agent demo client")
    parser.add_argument("--dev", action="store_true", help="Rely on server DEV_MODE (no wallet).")
    parser.add_argument("--url", default=None, help="API base URL (default PREFLIGHT_API_URL).")
    parser.add_argument(
        "--route",
        choices=sorted(ROUTES),
        default=DEFAULT_ROUTE,
        help="Paid route to call (default scan).",
    )
    parser.add_argument("--source", default=None, help="Solidity source string to scan.")
    parser.add_argument(
        "--file",
        action="append",
        default=None,
        help="Path to a .sol file (repeat for --route project).",
    )
    parser.add_argument("--contract-name", default=None, help="Target contract name.")
    parser.add_argument("--to", default=None, help="Simulation target address (--route sim).")
    parser.add_argument("--data", default=None, help="0x-hex calldata (--route sim).")
    parser.add_argument("--value-wei", default=None, help="Wei value sent (--route sim).")
    parser.add_argument(
        "--from-address", default=None, help="Simulation sender override (--route sim)."
    )
    parser.add_argument("--address", default=None, help="Address to check (--route address-risk).")
    return parser.parse_args()


def _payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Build the request body for the selected route from parsed CLI args."""
    files: dict[str, str] | None = None
    if route_is_project(args.route):
        if args.file:
            files = {
                Path(path).name: Path(path).read_text(encoding="utf-8") for path in args.file
            }
        elif args.source:
            files = {"Contract.sol": args.source}

    source = args.source
    if args.file and not route_is_project(args.route):
        source = Path(args.file[0]).read_text(encoding="utf-8")

    return build_payload(
        args.route,
        source=source,
        contract_name=args.contract_name,
        files=files,
        to=args.to,
        data=args.data,
        value_wei=args.value_wei,
        from_address=args.from_address,
        address=args.address,
    )


def route_is_project(route: str) -> bool:
    """Whether ``route`` takes a ``files`` map."""
    return route == "project"


def main() -> None:
    """Entry point for ``python client/pay_client.py``."""
    args = _parse_args()

    from preflight.config import config

    api_url = args.url or config().api_url
    route = ROUTES[args.route]
    try:
        payload = _payload_from_args(args)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc

    if args.dev:
        print(f"=== dev mode: POST {api_url}{route.path} ({route.price}, server bypass) ===")
        report = post_dev(api_url, route.path, payload)
        print_result(args.route, report, None)
        return

    private_key_hex, created = load_or_create_key(ENV_FILE)
    account = Account.from_key(private_key_hex)
    if created:
        print("generated burner key and saved to .env")
    print(f"burner address: {account.address}")

    rpc_url = config().rpc_url or DEFAULT_RPC_URL
    balance = get_usdc_balance(account.address, rpc_url)
    print(f"USDC balance:   {balance / 10**USDC_DECIMALS:.6f} ({balance} base units)")

    if balance <= 0:
        _print_faucet_instructions(account.address)
        return

    print(f"=== full x402 flow: POST {api_url}{route.path} ({route.price}) ===")
    report, settle = post_paid(api_url, route.path, payload, private_key_hex)
    print_result(args.route, report, settle)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
