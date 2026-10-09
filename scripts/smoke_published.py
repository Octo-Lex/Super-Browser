#!/usr/bin/env python3
"""Post-release smoke test for published PyPI package.

Installs ``superbrowser-sdk`` from PyPI into a fresh virtual environment
and validates:

1. Installation succeeds for all extras (``[all]``, ``[patchright]``,
   ``[playwright]``)
2. ``import super_browser`` works
3. ``superbrowser version`` CLI works
4. ``superbrowser info`` CLI works
5. ``__version__`` matches expected version

Outputs a JSON smoke report and exits non-zero on any failure.

Usage::

    python scripts/smoke_published.py [--version 2.0.2] [--out smoke-report.json]

By default checks the latest published version.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_DIST = "superbrowser-sdk"
DEFAULT_OUT = "smoke-report.json"


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def _create_venv(path: Path) -> Path:
    """Create a virtual environment and return the python executable path."""
    rc, _, err = _run([sys.executable, "-m", "venv", str(path)])
    if rc != 0:
        raise RuntimeError(f"Failed to create venv: {err}")

    if platform.system() == "Windows":
        return path / "Scripts" / "python.exe"
    return path / "bin" / "python"


def _write_report(report: dict[str, Any], out_path: Path) -> None:
    """Write the smoke report JSON, creating parent dirs if needed."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)


def _check_result(name: str, rc: int, stdout: str, stderr: str) -> dict[str, Any]:
    """Build a check result dict.

    rc == 124 is this harness's own timeout sentinel: the check is
    classified ``install_timeout`` (infrastructure), distinct from a real
    SDK defect, so a slow download is never mistaken for a broken package.
    """
    # P2-review: classify timeouts by OPERATION, not exit code alone — a
    # hung import or CLI invocation is an SDK defect even though the
    # harness surfaces it as a timeout. Only installation operations get
    # the infrastructure classification.
    if rc == 124 and name.startswith("install"):
        classification = "install_timeout"
    else:
        classification = "sdk_defect"
    return {
        "name": name,
        "passed": rc == 0,
        "exit_code": rc,
        "classification": classification,
        "stdout": stdout[:2000],  # truncate long output
        "stderr": stderr[:2000],
    }


def run_smoke(
    *,
    version: str | None,
    dist: str,
    out_path: Path,
) -> dict[str, Any]:
    """Run the full smoke test suite in a fresh venv.

    Returns the complete smoke report dict.
    """
    checks: list[dict[str, Any]] = []
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Determine install spec — extras go before version: dist[extra]==version
    version_suffix = f"=={version}" if version else ""
    install_spec = f"{dist}{version_suffix}"  # for display only

    with tempfile.TemporaryDirectory(prefix="sb_smoke_") as tmpdir:
        tmp = Path(tmpdir)
        try:
            venv_python = _create_venv(tmp / "venv")
        except RuntimeError as exc:
            checks.append(_check_result("venv_creation", 1, "", str(exc)))
            report = _build_report(started, install_spec, checks)
            _write_report(report, out_path)
            return report

        # Upgrade pip
        rc, out, err = _run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])
        checks.append(_check_result("pip_upgrade", rc, out, err))

        # Install [all]
        # CI-7.1: 600s — a cold-cache [all] resolution on a fresh venv can
        # legitimately exceed 180s; the window must not expire into a false
        # SDK failure. A finite boundary is retained.
        rc, out, err = _run(
            [str(venv_python), "-m", "pip", "install", f"{dist}[all]{version_suffix}"],
            timeout=600,
        )
        checks.append(_check_result(f"install_all ({dist}[all]{version_suffix})", rc, out, err))
        if rc != 0:
            # No point continuing if base install fails
            report = _build_report(started, install_spec, checks)
            _write_report(report, out_path)
            return report

        # Import check
        rc, out, err = _run([
            str(venv_python), "-c",
            "import super_browser; print(super_browser.__version__)",
        ])
        checks.append(_check_result("import_super_browser", rc, out, err))

        # Record installed version
        installed_version = out.strip() if rc == 0 else None

        # CLI: version
        cli_exe = str(tmp / "venv" / ("Scripts" if platform.system() == "Windows" else "bin") / "superbrowser")
        rc, out, err = _run([cli_exe, "version"])
        checks.append(_check_result("cli_version", rc, out, err))

        # CLI: info
        rc, out, err = _run([cli_exe, "info"])
        checks.append(_check_result("cli_info", rc, out, err))

        # Verify [patchright] extra installs without conflict
        rc, out, err = _run(
            [str(venv_python), "-m", "pip", "install", f"{dist}[patchright]{version_suffix}"],
            timeout=600,
        )
        checks.append(_check_result(f"install_patchright ({dist}[patchright]{version_suffix})", rc, out, err))

        # Verify [playwright] extra installs without conflict
        rc, out, err = _run(
            [str(venv_python), "-m", "pip", "install", f"{dist}[playwright]{version_suffix}"],
            timeout=600,
        )
        checks.append(_check_result(f"install_playwright ({dist}[playwright]{version_suffix})", rc, out, err))

        # Version match check (if version was specified)
        if version and installed_version:
            version_match = version == installed_version
            checks.append({
                "name": "version_match",
                "passed": version_match,
                "expected": version,
                "actual": installed_version,
                "exit_code": 0 if version_match else 1,
                "stdout": f"expected={version} actual={installed_version}",
                "stderr": "",
            })

    report = _build_report(started, install_spec, checks)
    _write_report(report, out_path)
    return report


def _overall(checks: list[dict[str, Any]], passed: int, total: int) -> str:
    """Classify the run: PASS, INCONCLUSIVE, or FAIL.

    INCONCLUSIVE means every failure was this harness's own install timeout
    (infrastructure, not an SDK defect) — a slow download is never reported
    as a broken package. FAIL means at least one check failed for an SDK
    reason. A finite failure boundary is retained either way.
    """
    failures = [c for c in checks if not c["passed"]]
    if not failures:
        return "PASS"
    if all(c.get("classification") == "install_timeout" for c in failures):
        return "INCONCLUSIVE"
    return "FAIL"


def _build_report(started: str, install_spec: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the final report dict."""
    total = len(checks)
    passed = sum(1 for c in checks if c["passed"])
    return {
        "schema_version": 1,
        "timestamp_utc": started,
        "install_spec": install_spec,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "summary": {
            "total_checks": total,
            "passed": passed,
            "failed": total - passed,
            "overall": _overall(checks, passed, total),
        },
        "checks": checks,
    }


def format_markdown(report: dict[str, Any]) -> str:
    """Format the smoke report as Markdown."""
    summary = report["summary"]
    lines = [
        "# Post-Release Smoke Report",
        "",
        f"- **Timestamp:** {report['timestamp_utc']}",
        f"- **Install spec:** `{report['install_spec']}`",
        f"- **Result:** **{summary['overall']}**",
        f"- **Checks:** {summary['passed']}/{summary['total_checks']} passed",
        "",
        "| Check | Result | Details |",
        "|:------|:------:|:--------|",
    ]
    for c in report["checks"]:
        status = "✅" if c["passed"] else "❌"
        detail = c.get("stdout", c.get("stderr", ""))[:80]
        lines.append(f"| {c['name']} | {status} | {detail} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="smoke_published",
        description="Post-release smoke test for published PyPI package",
    )
    parser.add_argument("--version", type=str, default=None, help="Specific version to test")
    parser.add_argument("--out", type=Path, default=Path(DEFAULT_OUT), help="JSON output path")
    parser.add_argument("--md", type=Path, default=None, help="Markdown output path")
    parser.add_argument("--dist", type=str, default=DEFAULT_DIST, help="Distribution name")
    args = parser.parse_args()

    print("Running post-release smoke test...")
    print(f"  Distribution: {args.dist}")
    print(f"  Version: {args.version or 'latest'}")
    print()

    report = run_smoke(
        version=args.version,
        dist=args.dist,
        out_path=args.out,
    )

    md = format_markdown(report)
    print(md)

    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(md, encoding="utf-8")
        print(f"Markdown written to {args.md}")

    print(f"JSON written to {args.out}")

    summary = report["summary"]
    if summary["overall"] == "INCONCLUSIVE":
        print(
            "\n⚠️ Install window exceeded (INCONCLUSIVE) — infrastructure, "
            "not an SDK defect. Retry or extend the install timeout.",
            file=sys.stderr,
        )
        sys.exit(2)
    if summary["failed"] > 0:
        print(f"\n❌ {summary['failed']} check(s) failed", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"\n✅ All {summary['passed']} checks passed")


if __name__ == "__main__":
    main()
