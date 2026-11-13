"""Multi-file project analysis for Preflight."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from preflight.core.detectors import DetectorContext, run_all_custom_detectors
from preflight.core.report import build_scan_report
from preflight.types import Finding, ScanReport

from .pipeline import _SCAN_SLOTS, _get_solc_binary_path, _parse_slither_json


def _run_slither_on_project(
    main_file: str, solc_version: str, tmpdir: Path
) -> tuple[list[Finding], str | None]:
    """Run Slither on a multi-file project in a temp directory."""
    findings: list[Finding] = []
    note: str | None = None

    try:
        solc_binary = _get_solc_binary_path(solc_version)
        cmd = [
            "slither",
            str(tmpdir / main_file),
            "--json", "-",
            "--solc", solc_binary,
            "--solc-args", f'--allow-paths "{tmpdir}"',
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # If slither returns non-zero and no stdout, it's likely a compilation error
        if result.returncode != 0 and not result.stdout.strip():
            cmd_no_json = [
                "slither",
                str(tmpdir / main_file),
                "--solc", solc_binary,
                "--solc-args", f'--allow-paths "{tmpdir}"',
            ]
            result_no_json = subprocess.run(
                cmd_no_json,
                capture_output=True,
                text=True,
                timeout=30,
            )
            error_msg = result_no_json.stderr or result_no_json.stdout or "Unknown compilation error"
            raise ValueError(f"Solidity compilation failed: {error_msg}")

        if result.stdout:
            try:
                data = json.loads(result.stdout)
                if not data.get("success", True):
                    error = data.get("error", "Unknown compilation error")
                    raise ValueError(f"Solidity compilation failed: {error}")
                # Parse with the main source for snippet extraction
                main_source = (tmpdir / main_file).read_text(encoding="utf-8")
                findings = _parse_slither_json(data, main_source)
            except json.JSONDecodeError:
                pass

    except subprocess.TimeoutExpired:
        note = "Slither timed out after 120s; running custom detectors only"
    except FileNotFoundError:
        note = "Slither not available; running custom detectors only"
    except ValueError:
        raise
    except Exception as exc:
        note = f"Slither failed: {exc}; running custom detectors only"

    return findings, note


def run_analysis_project(
    files: dict[str, str],
    contract_name: str | None = None,
    solc_version: str | None = None,
) -> ScanReport:
    """Run security analysis on a multi-file Solidity project.

    For v1, we write all files to a temp directory and run slither on the main file.
    This allows imports to be resolved correctly.
    """
    if not files:
        raise ValueError("No files provided for project analysis")

    # Serialize heavy slither/solc work to protect the event loop from bursts.
    with _SCAN_SLOTS:
        # Auto-detect solc version if not provided
        if solc_version is None:
            # Use first file's pragma
            first_source = list(files.values())[0]
            from .pipeline import _extract_pragma_version, _ensure_solc_installed
            pragma = _extract_pragma_version(first_source)
            if pragma:
                solc_version = _ensure_solc_installed(pragma)
            else:
                solc_version = "0.8.20"
        else:
            from .pipeline import _ensure_solc_installed
            solc_version = _ensure_solc_installed(solc_version)

        # Determine the main file (first one, or the one matching contract_name)
        main_file = list(files.keys())[0]
        if contract_name:
            for fname, fsource in files.items():
                if f"contract {contract_name}" in fsource:
                    main_file = fname
                    break

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Write all files to temp directory
            for fname, fsource in files.items():
                file_path = tmpdir_path / fname
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(fsource, encoding="utf-8")

            # Run slither on the main file with all project files available
            slither_findings, slither_note = _run_slither_on_project(
                main_file, solc_version, tmpdir_path
            )

    # Run custom detectors on combined source (for cross-file patterns)
    combined_source = "\n\n".join(
        f"// File: {fname}\n{fsource}"
        for fname, fsource in sorted(files.items())
    )
    custom_findings = run_all_custom_detectors(
        DetectorContext(
            source=combined_source,
            lines=combined_source.splitlines(),
            contract_name=contract_name,
            solc_version=solc_version,
        )
    )

    notes = [slither_note] if slither_note else []

    # Build report
    report = build_scan_report(
        source=combined_source,
        slither_findings=slither_findings,
        custom_findings=custom_findings,
        contract_name=contract_name,
        solc_version=solc_version,
        notes=notes,
    )

    return report