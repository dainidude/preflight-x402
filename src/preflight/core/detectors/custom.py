"""Custom DeFi detectors for Preflight.

Each detector returns Findings with detector="custom:<name>".
Text/AST heuristics are fine for v1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from preflight.types import Finding, Severity


@dataclass
class DetectorContext:
    """Context passed to detectors with parsed source info."""
    source: str
    lines: list[str]
    contract_name: str | None
    solc_version: str | None
    interface_ranges: list[tuple[int, int]] = field(default_factory=list)


def _find_interface_ranges(source: str) -> list[tuple[int, int]]:
    """Return line-index ranges (start inclusive, end exclusive) of interface blocks."""
    ranges: list[tuple[int, int]] = []
    in_interface = False
    depth = 0
    start = 0
    for i, line in enumerate(source.splitlines()):
        stripped = line.strip()
        if not in_interface:
            if re.search(r"\binterface\b", stripped):
                in_interface = True
                start = i
                depth = stripped.count("{") - stripped.count("}")
                if depth <= 0 and "{" in stripped:
                    ranges.append((start, i + 1))
                    in_interface = False
        else:
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                ranges.append((start, i + 1))
                in_interface = False
    return ranges


def _inside_interface(ctx: DetectorContext, line_no: int) -> bool:
    """Check whether a line index sits inside an interface block."""
    return any(start <= line_no < end for start, end in ctx.interface_ranges)


def _is_function_declaration(line: str) -> bool:
    """True for function signatures without a body (interface/abstract/virtual)."""
    stripped = line.strip()
    return (
        "function" in stripped
        and stripped.endswith(";")
        and "{" not in stripped
    )


def _is_comment_line(line: str) -> bool:
    """True for pure comment lines."""
    stripped = line.strip()
    return stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*")


def _make_finding(
    detector_name: str,
    finding_id: str,
    title: str,
    severity: Severity,
    confidence: float,
    description: str,
    snippet: str | None,
    line_no: int | None = None,
    reference: str | None = None,
) -> Finding:
    """Helper to create a Finding with consistent format."""
    return Finding(
        id=f"custom:{detector_name}:{finding_id}",
        title=title,
        severity=severity,
        confidence=confidence,
        description=description,
        snippet=snippet,
        detector=f"custom:{detector_name}",
        reference=reference,
    )


def _extract_snippet(lines: list[str], line_no: int, radius: int = 3) -> str:
    """Extract a source snippet around a line number."""
    start = max(0, line_no - radius)
    end = min(len(lines), line_no + radius + 1)
    return "\n".join(f"{i+1:4d}  {lines[i]}" for i in range(start, end))


def detect_fee_on_transfer(ctx: DetectorContext) -> list[Finding]:
    """Detect fee-on-transfer token logic.

    Looks for patterns like:
    - balanceBefore/balanceAfter with transfer
    - _transferFee, _taxFee, _liquidityFee patterns
    - transfer amount != amount received
    """
    findings = []
    source_lower = ctx.source.lower()

    # Common fee-on-transfer patterns
    fee_patterns = [
        (r"_transferfee|_taxfee|_liquidityfee|_reflectfee", "Fee variable naming suggests fee-on-transfer"),
        (r"balance\s*[<>=!]+\s*balance", "Balance comparison around transfer"),
        (r"amount\s*[<>!]=\s*\w+\s*[-+*/]\s*\w+", "Amount modified before/after transfer"),
    ]

    for pattern, desc in fee_patterns:
        matches = list(re.finditer(pattern, source_lower))
        for m in matches:
            line_no = ctx.source[: m.start()].count("\n")
            if _inside_interface(ctx, line_no) or _is_comment_line(ctx.lines[line_no]):
                continue
            snippet = _extract_snippet(ctx.lines, line_no)
            findings.append(
                _make_finding(
                    detector_name="fee-on-transfer",
                    finding_id=f"fee-pattern-{len(findings)}",
                    title="Potential fee-on-transfer token logic",
                    severity="medium",
                    confidence=0.65,
                    description=f"{desc}: {m.group()}",
                    snippet=snippet,
                    line_no=line_no,
                    reference="https://github.com/ethereum/EIPs/issues/1363",
                )
            )

    # Look for transferFrom/transfer with amount adjustment
    transfer_adjustment = re.finditer(
        r"transfer(From)?\s*\([^)]*\)\s*[<>!]=", source_lower
    )
    for m in transfer_adjustment:
        line_no = ctx.source[: m.start()].count("\n")
        if _inside_interface(ctx, line_no) or _is_comment_line(ctx.lines[line_no]):
            continue
        snippet = _extract_snippet(ctx.lines, line_no)
        findings.append(
            _make_finding(
                detector_name="fee-on-transfer",
                finding_id=f"transfer-adjust-{len(findings)}",
                title="Transfer amount adjusted before/after call",
                severity="medium",
                confidence=0.6,
                description="Transfer call followed by comparison suggesting fee deduction",
                snippet=snippet,
                line_no=line_no,
            )
        )

    return findings


def detect_unguarded_initializer(ctx: DetectorContext) -> list[Finding]:
    """Detect unguarded initializers and UUPS without owner check.

    Patterns:
    - `initializer` modifier without access control
    - UUPS upgradeable without `__gap` or owner check in initialize
    - Constructor with `onlyOwner` missing on privileged functions
    """
    findings = []
    lines = ctx.lines

    # Find initializer functions - check for function with initializer modifier
    for i, line in enumerate(lines):
        if _inside_interface(ctx, i) or _is_function_declaration(line):
            continue
        line_lower = line.lower()
        # Check for function with initializer modifier on same line or next few lines
        if "function" in line_lower and ("initializer" in line_lower or "reinitializer" in line_lower):
            # Check if it has access control modifier (onlyOwner, onlyRole, etc.)
            # Look at the function declaration line and nearby lines for modifiers
            context_start = max(0, i - 2)
            context_end = min(len(lines), i + 3)
            context = " ".join(lines[context_start:context_end]).lower()
            
            has_access_control = any(
                kw in context
                for kw in ["onlyowner", "onlyrole", "accesscontrol", "auth"]
            )
            
            # Also check if the function name suggests it's an initializer but lacks the modifier
            # (handled in UUPS section below)
            
            if not has_access_control:
                snippet = _extract_snippet(lines, i)
                findings.append(
                    _make_finding(
                        detector_name="unguarded-initializer",
                        finding_id=f"init-{len(findings)}",
                        title="Unguarded initializer function",
                        severity="high",
                        confidence=0.85,
                        description="Initializer function lacks access control modifier (onlyOwner, onlyRole, etc.)",
                        snippet=snippet,
                        line_no=i,
                        reference="https://docs.openzeppelin.com/contracts/4.x/api/proxy#Initializable",
                    )
                )

    # Check for UUPS proxy without proper initialization protection
    if "uups" in ctx.source.lower() or "upgradeable" in ctx.source.lower():
        # Look for initialize() without initializer modifier
        for i, line in enumerate(lines):
            if _inside_interface(ctx, i) or _is_function_declaration(line):
                continue
            line_lower = line.lower()
            if "function initialize" in line_lower and "initializer" not in line_lower and "reinitializer" not in line_lower:
                # Check if it has any access control
                context_start = max(0, i - 2)
                context_end = min(len(lines), i + 5)
                context = " ".join(lines[context_start:context_end]).lower()
                has_access = any(
                    kw in context for kw in ["onlyowner", "onlyrole", "initializer", "reinitializer"]
                )
                if not has_access:
                    snippet = _extract_snippet(lines, i)
                    findings.append(
                        _make_finding(
                            detector_name="unguarded-initializer",
                            finding_id=f"uups-init-{len(findings)}",
                            title="UUPS initialize() missing initializer modifier",
                            severity="high",
                            confidence=0.8,
                            description="UUPS upgradeable contract has initialize() without initializer modifier or access control",
                            snippet=snippet,
                            line_no=i,
                            reference="https://docs.openzeppelin.com/contracts/4.x/api/proxy#UUPSUpgradeable",
                        )
                    )

    return findings


def detect_oracle_freshness(ctx: DetectorContext) -> list[Finding]:
    """Detect oracle freshness issues.

    Patterns:
    - latestRoundData() without updatedAt check
    - Chainlink oracle calls without staleness check
    - Direct use of price feeds without heartbeat validation
    """
    findings = []
    lines = ctx.lines
    source_lower = ctx.source.lower()

    # Find latestRoundData calls
    for i, line in enumerate(lines):
        if (
            _inside_interface(ctx, i)
            or _is_function_declaration(line)
            or _is_comment_line(line)
        ):
            continue
        if "latestrounddata" in line.lower():
            # Check surrounding context for updatedAt or timestamp check
            context_start = max(0, i - 5)
            context_end = min(len(lines), i + 10)
            context = " ".join(lines[context_start:context_end]).lower()

            has_freshness_check = any(
                kw in context
                for kw in ["updatedat", "timestamp", "block.timestamp", "heartbeat", "stale", "fresh"]
            )

            if not has_freshness_check:
                snippet = _extract_snippet(lines, i, radius=5)
                findings.append(
                    _make_finding(
                        detector_name="oracle-freshness",
                        finding_id=f"oracle-{len(findings)}",
                        title="Oracle latestRoundData() without freshness check",
                        severity="medium",
                        confidence=0.75,
                        description="Chainlink latestRoundData() called without checking updatedAt or heartbeat for staleness",
                        snippet=snippet,
                        line_no=i,
                        reference="https://docs.chain.link/data-feeds/historical-data#check-the-latest-round-data",
                    )
                )

    # Look for aggregatorV3Interface usage without validation
    if "aggregatorv3interface" in source_lower:
        for i, line in enumerate(lines):
            if _inside_interface(ctx, i) or _is_comment_line(line):
                continue
            if "aggregatorv3interface" in line.lower():
                snippet = _extract_snippet(lines, i)
                findings.append(
                    _make_finding(
                        detector_name="oracle-freshness",
                        finding_id=f"aggregator-{len(findings)}",
                        title="AggregatorV3Interface usage detected",
                        severity="info",
                        confidence=0.7,
                        description="Chainlink AggregatorV3Interface used; ensure freshness checks are implemented",
                        snippet=snippet,
                        line_no=i,
                    )
                )

    return findings


def detect_missing_zero_address_checks(ctx: DetectorContext) -> list[Finding]:
    """Detect missing zero-address checks on constructor/initializer params.

    Patterns:
    - Constructor/initializer parameters assigned to state variables without address(0) check
    - Owner, admin, feeReceiver, etc. set without validation
    """
    findings = []
    lines = ctx.lines

    # Common privileged parameter names
    privileged_params = [
        "owner",
        "admin",
        "fee_receiver",
        "feeReceiver",
        "treasury",
        "vault",
        "router",
        "factory",
        "implementation",
        "logic",
        "proxy_admin",
        "guardian",
        "pauser",
        "minter",
        "burner",
    ]

    # Find constructors and initializers
    for i, line in enumerate(lines):
        if _inside_interface(ctx, i) or _is_comment_line(line):
            continue
        line_lower = line.lower()
        is_constructor = "constructor" in line_lower
        is_initializer = "initializer" in line_lower and "function" in line_lower

        if is_constructor or is_initializer:
            # Look at the function signature and body
            sig_end = i
            while sig_end < len(lines) and "{" not in lines[sig_end]:
                sig_end += 1
            signature = " ".join(lines[i : sig_end + 1])

            # Check each privileged param
            for param in privileged_params:
                # Pattern: paramName = _paramName or this.paramName = paramName
                # Check if param (or _param) appears in signature with address type
                param_pattern = rf"(?:^|\s)_?{param}\s*[,)]"
                if re.search(param_pattern, signature, re.IGNORECASE) and "address" in signature.lower():
                    # Now check if there's a zero address check in the body (non-comment lines)
                    body_start = sig_end + 1
                    body_end = min(len(lines), body_start + 30)
                    body_lines = lines[body_start:body_end]
                    # Filter out comment lines
                    body_code = " ".join(
                        ln for ln in body_lines 
                        if not ln.strip().startswith("//") and not ln.strip().startswith("/*")
                    ).lower()

                    has_zero_check = any(
                        kw in body_code
                        for kw in [
                            f"{param} != address(0)",
                            f"{param} != address(0x0)",
                            f"require({param}",
                            f"_require({param}",
                            "address(0)",
                        ]
                    )

                    if not has_zero_check:
                        snippet = _extract_snippet(lines, i, radius=8)
                        findings.append(
                            _make_finding(
                                detector_name="missing-zero-check",
                                finding_id=f"zero-{param}-{len(findings)}",
                                title=f"Missing zero-address check for {param}",
                                severity="medium",
                                confidence=0.7,
                                description=f"Privileged address parameter '{param}' assigned without address(0) validation",
                                snippet=snippet,
                                line_no=i,
                                reference="https://swcregistry.io/docs/SWC-123",
                            )
                        )

    return findings


def detect_honeypot_patterns(ctx: DetectorContext) -> list[Finding]:
    """Detect honeypot patterns: owner can pause/blacklist transfers.

    Patterns:
    - pause() / unpause() with transfer restrictions
    - blacklist / whitelist mappings for addresses
    - _beforeTokenTransfer hooks with owner-controlled logic
    - transferFrom/transfer with role checks that owner controls
    """
    findings = []
    lines = ctx.lines
    source_lower = ctx.source.lower()

    # Check for Pausable with transfer restrictions
    has_pausable = "pausable" in source_lower or "whennotpaused" in source_lower
    if has_pausable:
        # Check if pause is owner-only or has broad control
        for i, line in enumerate(lines):
            if (
                _inside_interface(ctx, i)
                or _is_function_declaration(line)
                or _is_comment_line(line)
            ):
                continue
            if "function pause" in line.lower() or "function unpause" in line.lower():
                context = " ".join(lines[max(0, i - 2) : min(len(lines), i + 5)]).lower()
                if "onlyowner" in context or "onlyrole" in context:
                    snippet = _extract_snippet(lines, i)
                    findings.append(
                        _make_finding(
                            detector_name="honeypot",
                            finding_id=f"pause-{len(findings)}",
                            title="Owner-controlled pause can block transfers",
                            severity="high",
                            confidence=0.8,
                            description="Contract uses Pausable with owner-only pause/unpause; transfers can be blocked arbitrarily",
                            snippet=snippet,
                            line_no=i,
                            reference="https://github.com/ethereum/EIPs/blob/master/EIPS/eip-20.md#transfer",
                        )
                    )

    # Check for blacklist/whitelist mappings
    blacklist_patterns = [
        r"mapping\s*\(\s*address\s*=>\s*bool\s*\)\s+(?:public\s+)?(?:is)?(?:blacklist|blocked|banned|frozen)",
        r"mapping\s*\(\s*address\s*=>\s*bool\s*\)\s+(?:public\s+)?(?:is)?(?:whitelist|allowed|approved)",
        r"(?:blacklist|whitelist)\s*\[",
    ]

    for pattern in blacklist_patterns:
        matches = list(re.finditer(pattern, source_lower))
        for m in matches:
            line_no = ctx.source[: m.start()].count("\n")
            if _inside_interface(ctx, line_no) or _is_comment_line(ctx.lines[line_no]):
                continue
            snippet = _extract_snippet(ctx.lines, line_no)
            findings.append(
                _make_finding(
                    detector_name="honeypot",
                    finding_id=f"list-{len(findings)}",
                    title="Address blacklist/whitelist controls transfers",
                    severity="high",
                    confidence=0.75,
                    description="Contract maintains address allow/block list that can restrict transfers",
                    snippet=snippet,
                    line_no=line_no,
                    reference="https://consensys.github.io/smart-contract-best-practices/development-recommendations/general/restrict-token-transfers/",
                )
            )

    # Check for _beforeTokenTransfer with owner logic
    for i, line in enumerate(lines):
        if (
            _inside_interface(ctx, i)
            or _is_function_declaration(line)
            or _is_comment_line(line)
        ):
            continue
        if "_beforetokentransfer" in line.lower():
            context = " ".join(lines[i : min(len(lines), i + 20)]).lower()
            if "owner" in context or "onlyowner" in context or "blacklist" in context:
                snippet = _extract_snippet(lines, i, radius=10)
                findings.append(
                    _make_finding(
                        detector_name="honeypot",
                        finding_id=f"hook-{len(findings)}",
                        title="_beforeTokenTransfer with owner-controlled logic",
                        severity="high",
                        confidence=0.85,
                        description="Transfer hook contains owner-controlled restrictions (pause, blacklist, etc.)",
                        snippet=snippet,
                        line_no=i,
                    )
                )

    return findings


def run_all_custom_detectors(source: str | DetectorContext) -> list[Finding]:
    """Run all custom detectors and return combined findings."""
    if isinstance(source, str):
        ctx = DetectorContext(
            source=source,
            lines=source.splitlines(),
            contract_name=None,
            solc_version=None,
            interface_ranges=_find_interface_ranges(source),
        )
    else:
        ctx = source
        if not ctx.interface_ranges:
            ctx.interface_ranges = _find_interface_ranges(ctx.source)

    all_findings = []
    all_findings.extend(detect_fee_on_transfer(ctx))
    all_findings.extend(detect_unguarded_initializer(ctx))
    all_findings.extend(detect_oracle_freshness(ctx))
    all_findings.extend(detect_missing_zero_address_checks(ctx))
    all_findings.extend(detect_honeypot_patterns(ctx))
    return all_findings