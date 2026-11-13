"""Core analysis pipeline for Preflight.

Runs Solidity compilation with solcx, Slither static analysis,
and custom DeFi detectors.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

# Concurrency limit for slither/solc heavy work so bursts queue instead of
# exhausting the container.
_SCAN_SLOTS = threading.Semaphore(3)


import solcx

from preflight.core.detectors import DetectorContext, run_all_custom_detectors
from preflight.core.report import build_scan_report
from preflight.types import Finding, ScanReport, Severity


# Pre-installed solc versions (verified available)
PREINSTALLED_SOLC = ["0.8.20", "0.8.24", "0.8.26"]


def _ensure_solc_installed(pragma_version: str) -> str:
    """Ensure the required solc version is installed, return the version string.

    Args:
        pragma_version: Version constraint from pragma (e.g., "^0.8.20", "0.8.24")

    Returns:
        The solc version string to use for compilation
    """
    # Normalize pragma to a concrete version
    version_match = re.search(r"(\d+\.\d+\.\d+)", pragma_version)
    if not version_match:
        # Default to latest pre-installed
        return PREINSTALLED_SOLC[-1]

    target_version = version_match.group(1)

    # Check if exact version is pre-installed
    installed = solcx.get_installed_solc_versions()
    installed_strs = [str(v) for v in installed]

    if target_version in installed_strs:
        return target_version

    # Find compatible pre-installed version
    for v in reversed(PREINSTALLED_SOLC):
        if _version_satisfies(v, pragma_version):
            return v

    # Fallback: install the exact version
    try:
        solcx.install_solc(target_version)
        return target_version
    except Exception:
        # Last resort: use latest pre-installed
        return PREINSTALLED_SOLC[-1]


def _version_satisfies(version: str, constraint: str) -> bool:
    """Check if a version satisfies a pragma constraint (simplified)."""
    # Handle common constraints: ^0.8.20, >=0.8.20, ~0.8.20, 0.8.20
    constraint = constraint.strip()

    if constraint.startswith("^"):
        # ^0.8.20 means >=0.8.20 <0.9.0
        base = constraint[1:]
        base_parts = list(map(int, base.split(".")))
        ver_parts = list(map(int, version.split(".")))
        return (
            ver_parts[0] == base_parts[0]
            and ver_parts[1] == base_parts[1]
            and ver_parts[2] >= base_parts[2]
        )
    if constraint.startswith("~"):
        # ~0.8.20 means >=0.8.20 <0.8.21
        base = constraint[1:]
        base_parts = list(map(int, base.split(".")))
        ver_parts = list(map(int, version.split(".")))
        return (
            ver_parts[0] == base_parts[0]
            and ver_parts[1] == base_parts[1]
            and ver_parts[2] >= base_parts[2]
        )
    if constraint.startswith(">="):
        base = constraint[2:]
        return _compare_versions(version, base) >= 0
    if constraint.startswith(">"):
        base = constraint[1:]
        return _compare_versions(version, base) > 0
    if constraint.startswith("<="):
        base = constraint[2:]
        return _compare_versions(version, base) <= 0
    if constraint.startswith("<"):
        base = constraint[1:]
        return _compare_versions(version, base) < 0

    # Exact match
    return version == constraint


def _compare_versions(v1: str, v2: str) -> int:
    """Compare two version strings. Returns -1, 0, or 1."""
    p1 = list(map(int, v1.split(".")))
    p2 = list(map(int, v2.split(".")))
    for a, b in zip(p1, p2):
        if a < b:
            return -1
        if a > b:
            return 1
    return 0


def _extract_pragma_version(source: str) -> str | None:
    """Extract Solidity pragma version from source."""
    # Match pragma solidity ^0.8.20; or pragma solidity >=0.8.20 <0.9.0;
    patterns = [
        r"pragma\s+solidity\s+([^;]+);",
        r"pragma\s+solidity\s*([^\n;]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, source, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _get_solc_binary_path(version: str) -> str:
    """Get the path to the solc binary for a given version."""
    import solcx
    solcx.set_solc_version(version)
    install_folder = solcx.get_solcx_install_folder()
    # Find the solc binary for this version
    for f in os.listdir(install_folder):
        if f"solc-v{version}" == f or f.startswith(f"solc-v{version}"):
            return os.path.join(install_folder, f)
    # Fallback: just return the version and let slither find it
    return version


def _run_slither_on_source(source: str, solc_version: str) -> tuple[list[Finding], str | None]:
    """Run Slither on source code via subprocess and return findings + status note.

    Uses temporary file approach for robustness. A note is returned when Slither
    is missing or times out so callers can degrade gracefully.
    """
    findings: list[Finding] = []
    note: str | None = None

    with tempfile.TemporaryDirectory() as tmpdir:
        source_file = Path(tmpdir) / "contract.sol"
        source_file.write_text(source, encoding="utf-8")

        try:
            # Run slither CLI with JSON output
            solc_binary = _get_solc_binary_path(solc_version)
            cmd = [
                "slither",
                str(source_file),
                "--json", "-",
                "--solc", solc_binary,
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )

            # If slither returns non-zero and no stdout, it's likely a compilation error
            if result.returncode != 0 and not result.stdout.strip():
                # Try running without --json to get error details
                cmd_no_json = [
                    "slither",
                    str(source_file),
                    "--solc", solc_binary,
                ]
                result_no_json = subprocess.run(
                    cmd_no_json,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                error_msg = result_no_json.stderr or result_no_json.stdout or "Unknown compilation error"
                raise ValueError(f"Solidity compilation failed: {error_msg}")

            # Slither exits with non-zero on findings, so don't check returncode
            if result.stdout:
                try:
                    data = json.loads(result.stdout)
                    # Check for compilation errors in JSON output
                    if not data.get("success", True):
                        error = data.get("error", "Unknown compilation error")
                        raise ValueError(f"Solidity compilation failed: {error}")
                    findings = _parse_slither_json(data, source)
                except json.JSONDecodeError:
                    pass

        except subprocess.TimeoutExpired:
            note = "Slither timed out after 120s; running custom detectors only"
        except FileNotFoundError:
            note = "Slither not available; running custom detectors only"
        except ValueError:
            # Re-raise ValueError for invalid solidity
            raise
        except Exception as exc:
            note = f"Slither failed: {exc}; running custom detectors only"

    return findings, note


def _parse_slither_json(data: dict[str, Any], source: str) -> list[Finding]:
    """Parse Slither JSON output into Finding objects."""
    findings: list[Finding] = []
    lines = source.splitlines()

    results = data.get("results", {})
    detectors = results.get("detectors", [])

    for det in detectors:
        severity = _map_slither_severity(det.get("impact", "info"))
        confidence = _map_slither_confidence(det.get("confidence", "medium"))

        # Extract snippet
        elements = det.get("elements", [])
        snippet = None
        line_no = None
        if elements:
            first_elem = elements[0]
            if "source_mapping" in first_elem:
                sm = first_elem["source_mapping"]
                line_no = sm.get("lines", [0])[0] - 1  # 0-indexed
                if line_no is not None and 0 <= line_no < len(lines):
                    snippet = "\n".join(
                        f"{i+1:4d}  {lines[i]}"
                        for i in range(max(0, line_no - 2), min(len(lines), line_no + 3))
                    )

        finding = Finding(
            id=f"slither:{det.get('check', 'unknown')}",
            title=det.get("description", "Slither finding").split("\n")[0][:100],
            severity=severity,
            confidence=confidence,
            description=det.get("description", ""),
            snippet=snippet,
            detector="slither",
            reference=det.get("reference"),
        )
        findings.append(finding)

    return findings


def _map_slither_severity(sev: str) -> Severity:
    """Map Slither severity to our severity enum."""
    mapping: dict[str, Severity] = {
        "critical": "critical",
        "high": "high",
        "medium": "medium",
        "low": "low",
        "informational": "info",
        "optimization": "info",
    }
    return mapping.get(sev.lower(), "info")


def _map_slither_confidence(conf: str) -> float:
    """Map Slither confidence to float."""
    mapping = {
        "high": 0.9,
        "medium": 0.7,
        "low": 0.5,
    }
    return mapping.get(conf.lower(), 0.5)


def _run_custom_detectors(source: str, contract_name: str | None, solc_version: str | None) -> list[Finding]:
    """Run all custom DeFi detectors."""
    ctx = DetectorContext(
        source=source,
        lines=source.splitlines(),
        contract_name=contract_name,
        solc_version=solc_version,
    )
    return run_all_custom_detectors(ctx)


def run_analysis(
    source: str,
    contract_name: str | None = None,
    solc_version: str | None = None,
) -> ScanReport:
    """Run complete security analysis on Solidity source code.

    Args:
        source: Solidity source code (single file)
        contract_name: Target contract name if source has multiple contracts
        solc_version: Solidity compiler version (auto-detected from pragma if omitted)

    Returns:
        ScanReport with security findings and verdict
    """
    if not source or not source.strip():
        raise ValueError("Empty source code provided")

    # Serialize heavy slither/solc work to protect the event loop from bursts.
    with _SCAN_SLOTS:
        # Auto-detect solc version from pragma if not provided
        if solc_version is None:
            pragma = _extract_pragma_version(source)
            if pragma:
                solc_version = _ensure_solc_installed(pragma)
            else:
                solc_version = PREINSTALLED_SOLC[-1]
        else:
            # Ensure the specified version is available
            solc_version = _ensure_solc_installed(solc_version)

        # Run Slither analysis
        slither_findings, slither_note = _run_slither_on_source(source, solc_version)

    # Run custom detectors
    custom_findings = _run_custom_detectors(source, contract_name, solc_version)

    notes = [slither_note] if slither_note else []

    # Build final report
    report = build_scan_report(
        source=source,
        slither_findings=slither_findings,
        custom_findings=custom_findings,
        contract_name=contract_name,
        solc_version=solc_version,
        notes=notes,
    )

    return report