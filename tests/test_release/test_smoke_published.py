"""Unit tests for the post-release smoke test script.

Tests report building, check result construction, Markdown rendering,
and CLI arg parsing — without requiring actual PyPI installation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "smoke_published.py"

sys.path.insert(0, str(SCRIPT.parent))
import smoke_published as sp  # noqa: E402  type: ignore[import-not-found]


class TestCheckResult:
    """Test _check_result()."""

    def test_passing_check(self) -> None:
        result = sp._check_result("test_check", 0, "success output", "")
        assert result["name"] == "test_check"
        assert result["passed"] is True
        assert result["exit_code"] == 0
        assert result["stdout"] == "success output"

    def test_failing_check(self) -> None:
        result = sp._check_result("test_check", 1, "", "error message")
        assert result["passed"] is False
        assert result["exit_code"] == 1
        assert result["stderr"] == "error message"

    def test_long_output_truncated(self) -> None:
        """Output longer than 2000 chars should be truncated."""
        long_output = "x" * 3000
        result = sp._check_result("test", 0, long_output, "")
        assert len(result["stdout"]) <= 2000

    def test_timeout_exit_code(self) -> None:
        result = sp._check_result("test", 124, "", "timed out")
        assert result["passed"] is False
        assert result["exit_code"] == 124


class TestBuildReport:
    """Test _build_report()."""

    def _make_checks(self) -> list[dict[str, Any]]:
        return [
            sp._check_result("check1", 0, "ok", ""),
            sp._check_result("check2", 0, "ok", ""),
            sp._check_result("check3", 1, "", "fail"),
        ]

    def test_report_structure(self) -> None:
        report = sp._build_report("2026-01-01T00:00:00Z", "superbrowser-sdk", self._make_checks())
        assert report["schema_version"] == 1
        assert "timestamp_utc" in report
        assert report["install_spec"] == "superbrowser-sdk"
        assert "environment" in report
        assert "summary" in report
        assert "checks" in report

    def test_summary_counts(self) -> None:
        report = sp._build_report("ts", "spec", self._make_checks())
        assert report["summary"]["total_checks"] == 3
        assert report["summary"]["passed"] == 2
        assert report["summary"]["failed"] == 1

    def test_overall_fail(self) -> None:
        report = sp._build_report("ts", "spec", self._make_checks())
        assert report["summary"]["overall"] == "FAIL"

    def test_overall_pass(self) -> None:
        checks = [sp._check_result("c", 0, "ok", "")]
        report = sp._build_report("ts", "spec", checks)
        assert report["summary"]["overall"] == "PASS"

    def test_empty_checks(self) -> None:
        report = sp._build_report("ts", "spec", [])
        assert report["summary"]["total_checks"] == 0
        assert report["summary"]["passed"] == 0
        assert report["summary"]["overall"] == "PASS"  # vacuously true

    def test_json_serializable(self) -> None:
        report = sp._build_report("ts", "spec", self._make_checks())
        serialized = json.dumps(report)
        assert json.loads(serialized)["schema_version"] == 1


class TestFormatMarkdown:
    """Test format_markdown()."""

    def _make_report(self) -> dict[str, Any]:
        return sp._build_report("2026-06-16T00:00:00Z", "superbrowser-sdk", [
            sp._check_result("import_super_browser", 0, "2.0.2", ""),
            sp._check_result("cli_version", 0, "superbrowser 2.0.2", ""),
            sp._check_result("install_patchright", 1, "", "conflict"),
        ])

    def test_renders_header(self) -> None:
        md = sp.format_markdown(self._make_report())
        assert "# Post-Release Smoke Report" in md
        assert "superbrowser-sdk" in md

    def test_renders_table(self) -> None:
        md = sp.format_markdown(self._make_report())
        assert "| Check | Result |" in md
        assert "import_super_browser" in md
        assert "cli_version" in md
        assert "install_patchright" in md

    def test_renders_pass_fail(self) -> None:
        md = sp.format_markdown(self._make_report())
        assert "✅" in md
        assert "❌" in md

    def test_renders_summary(self) -> None:
        md = sp.format_markdown(self._make_report())
        assert "FAIL" in md
        assert "2/3" in md

    def test_all_pass_renders_pass(self) -> None:
        report = sp._build_report("ts", "spec", [sp._check_result("c", 0, "ok", "")])
        md = sp.format_markdown(report)
        assert "**PASS**" in md


class TestSchemaValidation:
    """Validate smoke report schema shape."""

    def test_top_level_keys(self) -> None:
        report = sp._build_report("ts", "spec", [])
        for key in ("schema_version", "timestamp_utc", "install_spec", "environment", "summary", "checks"):
            assert key in report, f"Missing key: {key}"

    def test_check_keys(self) -> None:
        check = sp._check_result("test", 0, "out", "err")
        for key in ("name", "passed", "exit_code", "stdout", "stderr"):
            assert key in check, f"Missing check key: {key}"

    def test_summary_keys(self) -> None:
        report = sp._build_report("ts", "spec", [])
        summary = report["summary"]
        for key in ("total_checks", "passed", "failed", "overall"):
            assert key in summary, f"Missing summary key: {key}"


class TestConstants:
    """Test default constants."""

    def test_default_dist(self) -> None:
        assert sp.DEFAULT_DIST == "superbrowser-sdk"

    def test_default_out(self) -> None:
        assert sp.DEFAULT_OUT == "smoke-report.json"


class TestRunCommand:
    """Test _run() helper."""

    def test_successful_command(self) -> None:
        rc, out, err = sp._run([sys.executable, "-c", "print('hello')"])
        assert rc == 0
        assert "hello" in out

    def test_failing_command(self) -> None:
        rc, out, err = sp._run([sys.executable, "-c", "import sys; sys.exit(1)"])
        assert rc == 1

    def test_timeout(self) -> None:
        rc, out, err = sp._run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=1)
        assert rc == 124
        assert "timed out" in err.lower()


class TestVenvCreation:
    """Test _create_venv()."""

    def test_creates_python_exe(self, tmp_path: Path) -> None:
        venv_python = sp._create_venv(tmp_path / "venv")
        assert venv_python.exists()

    def test_python_works(self, tmp_path: Path) -> None:
        venv_python = sp._create_venv(tmp_path / "venv")
        rc, out, err = sp._run([str(venv_python), "--version"])
        assert rc == 0
        assert "Python" in out


class TestWriteReport:
    """Test _write_report() helper."""

    def test_writes_valid_json(self, tmp_path: Path) -> None:
        report = sp._build_report("ts", "spec", [sp._check_result("c", 0, "ok", "")])
        out = tmp_path / "results" / "report.json"
        sp._write_report(report, out)
        assert out.exists()
        loaded = json.loads(out.read_text())
        assert loaded["schema_version"] == 1

    def test_creates_nested_dirs(self, tmp_path: Path) -> None:
        report = sp._build_report("ts", "spec", [])
        out = tmp_path / "a" / "b" / "c" / "report.json"
        sp._write_report(report, out)
        assert out.exists()


class TestEarlyFailurePaths:
    """Test that JSON report is always written, even on early failures.

    Regression guard for PR #156 review: venv creation failure and
    [all] install failure previously returned without writing JSON.
    """

    def test_venv_creation_failure_writes_json(self, tmp_path: Path) -> None:
        """Venv creation failure must still write the JSON report."""
        from unittest.mock import patch

        out_path = tmp_path / "smoke.json"

        with patch.object(sp, "_create_venv", side_effect=RuntimeError("venv failed")):
            sp.run_smoke(version="2.0.2", dist="superbrowser-sdk", out_path=out_path)

        # JSON must be written
        assert out_path.exists()
        loaded = json.loads(out_path.read_text())
        assert loaded["summary"]["overall"] == "FAIL"
        assert loaded["summary"]["failed"] >= 1

        # The venv_creation check should be in the report
        check_names = [c["name"] for c in loaded["checks"]]
        assert "venv_creation" in check_names

    def test_install_all_failure_writes_json(self, tmp_path: Path) -> None:
        """[all] install failure must still write the JSON report."""
        from unittest.mock import patch

        out_path = tmp_path / "smoke.json"

        # Mock venv creation to succeed, but [all] install to fail
        def mock_run(cmd: list[str], **kwargs: Any) -> tuple[int, str, str]:
            if "venv" in str(cmd):
                return 0, "", ""
            if "pip" in cmd and "install" in cmd and "--upgrade" in cmd:
                return 0, "pip upgraded", ""
            if "[all]" in " ".join(cmd):
                return 1, "", "ERROR: Package not found"
            return 0, "", ""

        with (
            patch.object(sp, "_create_venv", return_value=Path("/fake/python")),
            patch.object(sp, "_run", side_effect=mock_run),
        ):
            sp.run_smoke(version="2.0.2", dist="superbrowser-sdk", out_path=out_path)

        # JSON must be written
        assert out_path.exists()
        loaded = json.loads(out_path.read_text())
        assert loaded["summary"]["overall"] == "FAIL"

        # Should have at least pip_upgrade and install_all checks
        check_names = [c["name"] for c in loaded["checks"]]
        assert any("install_all" in name for name in check_names)


class TestInstallSpecFormat:
    """Test that pip install spec uses correct syntax.

    Regression guard for v2.1.0 post-release smoke failure: extras
    must go before the version specifier (dist[extra]==version),
    not after (dist==version[extra]).
    """

    def test_extras_before_version(self, tmp_path: Path) -> None:
        """pip install args must format as dist[extra]==version."""
        from unittest.mock import patch

        captured_cmds: list[list[str]] = []

        def mock_run(cmd: list[str], **kwargs: Any) -> tuple[int, str, str]:
            captured_cmds.append(cmd)
            if "venv" in str(cmd):
                return 0, "", ""
            if "--upgrade" in cmd:
                return 0, "", ""
            # Simulate success for all installs
            if "install" in cmd:
                return 0, "installed", ""
            # Simulate import / CLI success
            return 0, "2.1.0", ""

        out_path = tmp_path / "smoke.json"

        with (
            patch.object(sp, "_create_venv", return_value=Path("/fake/python")),
            patch.object(sp, "_run", side_effect=mock_run),
        ):
            sp.run_smoke(version="2.1.0", dist="superbrowser-sdk", out_path=out_path)

        # Find the install commands
        install_cmds = [c for c in captured_cmds if "install" in c and "--upgrade" not in c]
        assert len(install_cmds) >= 3  # [all], [patchright], [playwright]

        for cmd in install_cmds:
            # Get the package spec (last argument before timeout)
            pkg_arg = [a for a in cmd if "superbrowser-sdk" in a][0]
            # Must be: superbrowser-sdk[extra]==2.1.0
            # NOT: superbrowser-sdk==2.1.0[extra]
            assert "==" in pkg_arg, f"Missing version in: {pkg_arg}"
            assert "[" in pkg_arg, f"Missing extras in: {pkg_arg}"
            # The bracket must come before ==
            bracket_pos = pkg_arg.index("[")
            eq_pos = pkg_arg.index("==")
            assert bracket_pos < eq_pos, f"Extras must come before version: {pkg_arg}"


# ============================================================================
# CI-7.1 — install-timeout classification and INCONCLUSIVE boundary
# ============================================================================


class TestInstallTimeoutClassification:
    """A slow download is infrastructure (install_timeout), never an SDK
    defect. A finite failure boundary is retained via the exit code."""

    def test_timeout_check_classified_install_timeout(self) -> None:
        result = sp._check_result("install_all", 124, "", "Command timed out after 600s")
        assert result["classification"] == "install_timeout"
        assert result["passed"] is False

    def test_non_timeout_failure_classified_sdk_defect(self) -> None:
        result = sp._check_result("install_all", 1, "", "ERROR: Package not found")
        assert result["classification"] == "sdk_defect"

    def test_passing_check_classified_none(self) -> None:
        result = sp._check_result("import_super_browser", 0, "2.14.0", "")
        assert result["classification"] == "sdk_defect"
        assert result["passed"] is True

    def test_all_timeout_failures_report_inconclusive(self) -> None:
        """Only-timeout failures are INCONCLUSIVE — infrastructure, not a
        broken package. The finite boundary stays: the CLI exits non-zero."""
        checks = [
            sp._check_result("pip_upgrade", 0, "upgraded", ""),
            sp._check_result("install_all", 124, "", "Command timed out after 600s"),
        ]
        report = sp._build_report("ts", "spec", checks)
        assert report["summary"]["overall"] == "INCONCLUSIVE"

    def test_sdk_defect_failure_still_reports_fail(self) -> None:
        checks = [
            sp._check_result("pip_upgrade", 0, "upgraded", ""),
            sp._check_result("install_all", 1, "", "ERROR: Package not found"),
        ]
        report = sp._build_report("ts", "spec", checks)
        assert report["summary"]["overall"] == "FAIL"

    def test_mixed_timeout_and_sdk_defect_reports_fail(self) -> None:
        """One real SDK failure outweighs an infrastructure timeout."""
        checks = [
            sp._check_result("install_all", 124, "", "Command timed out after 600s"),
            sp._check_result("import_super_browser", 1, "", "ImportError"),
        ]
        report = sp._build_report("ts", "spec", checks)
        assert report["summary"]["overall"] == "FAIL"

    def test_install_windows_are_600s(self) -> None:
        """CI-7.1: the [all] and extra-install windows are 600 seconds —
        a cold-cache [all] resolution can legitimately exceed 180s."""
        import inspect

        source = inspect.getsource(sp.run_smoke)
        assert "timeout=600" in source
        assert "timeout=180" not in source


class TestTimeoutClassificationByOperation:
    """P2-review: timeout classification is by OPERATION, not exit code
    alone. Installation timeouts are infrastructure; runtime timeouts
    (import, CLI) are SDK defects — a run containing only runtime timeouts
    must FAIL, not go INCONCLUSIVE."""

    def test_import_timeout_classified_sdk_defect(self) -> None:
        result = sp._check_result("import_super_browser", 124, "", "timed out after 120s")
        assert result["classification"] == "sdk_defect"

    def test_cli_timeout_classified_sdk_defect(self) -> None:
        result = sp._check_result("cli_version", 124, "", "timed out after 120s")
        assert result["classification"] == "sdk_defect"

    def test_install_timeouts_classified_install_timeout(self) -> None:
        for name in ("install_all (superbrowser-sdk[all]==2.14.0)",
                     "install_patchright (superbrowser-sdk[patchright]==2.14.0)",
                     "install_playwright (superbrowser-sdk[playwright]==2.14.0)"):
            result = sp._check_result(name, 124, "", "timed out")
            assert result["classification"] == "install_timeout", name

    def test_runtime_only_timeout_run_reports_fail(self) -> None:
        """Install succeeded; the SDK import hung. This is an SDK failure —
        the run must report FAIL, never INCONCLUSIVE."""
        checks = [
            sp._check_result("pip_upgrade", 0, "upgraded", ""),
            sp._check_result("install_all (superbrowser-sdk[all]==2.14.0)", 0, "installed", ""),
            sp._check_result("import_super_browser", 124, "", "timed out after 120s"),
        ]
        report = sp._build_report("ts", "spec", checks)
        assert report["summary"]["overall"] == "FAIL"

    def test_mixed_install_ok_and_import_timeout_reports_fail(self) -> None:
        checks = [
            sp._check_result("pip_upgrade", 0, "upgraded", ""),
            sp._check_result("install_all (superbrowser-sdk[all]==2.14.0)", 0, "installed", ""),
            sp._check_result("import_super_browser", 124, "", "timed out after 120s"),
            sp._check_result("cli_version", 124, "", "timed out after 120s"),
        ]
        report = sp._build_report("ts", "spec", checks)
        assert report["summary"]["overall"] == "FAIL"
        assert report["summary"]["failed"] == 2
