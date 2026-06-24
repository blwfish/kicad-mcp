"""
Tests for cli_drc.py — the KiCad CLI-based DRC implementation.

Covers all error branches, success paths, violation categorization fallbacks,
external-interface drift, and progress-reporting behaviour.
"""

import asyncio
import json
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kicad_mcp.tools.drc_impl.cli_drc import run_drc_via_cli
from kicad_mcp.utils.kicad_cli import KiCadCLIError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MODULE = "kicad_mcp.tools.drc_impl.cli_drc"


def _run(pcb_file: str, ctx=None):
    """Synchronous wrapper for the async function under test."""
    return asyncio.run(run_drc_via_cli(pcb_file, ctx))


def _make_subprocess_result(returncode=0, stdout="", stderr=""):
    """Build a fake CompletedProcess."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _write_json_side_effect(output_file, data):
    """Return a side_effect callable that writes JSON to output_file."""
    def _side_effect(*args, **kwargs):
        with open(output_file, "w") as f:
            json.dump(data, f)
        return _make_subprocess_result(returncode=0)
    return _side_effect


# ---------------------------------------------------------------------------
# Test: kicad-cli not found
# ---------------------------------------------------------------------------

class TestKiCadCLINotFound:

    def test_returns_success_false_when_cli_missing(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        with patch(f"{_MODULE}.get_kicad_cli_path", side_effect=KiCadCLIError("kicad-cli not found on this system")):
            result = _run(pcb)
        assert result["success"] is False
        assert result["method"] == "cli"
        assert result["pcb_file"] == pcb

    def test_error_message_contains_diagnostic_text(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        with patch(f"{_MODULE}.get_kicad_cli_path", side_effect=KiCadCLIError("kicad-cli not found on this system")):
            result = _run(pcb)
        assert "error" in result
        assert len(result["error"]) > 0

    def test_does_not_raise_on_cli_error(self, tmp_path):
        """Function must catch KiCadCLIError and return, not propagate."""
        pcb = str(tmp_path / "board.kicad_pcb")
        with patch(f"{_MODULE}.get_kicad_cli_path", side_effect=KiCadCLIError("missing")):
            # Must not raise
            result = _run(pcb)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Test: subprocess returncode != 0
# ---------------------------------------------------------------------------

class TestSubprocessFailure:

    def test_nonzero_returncode_returns_failure(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        fake_proc = _make_subprocess_result(returncode=1, stderr="PCB load error")
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", return_value=fake_proc):
            result = _run(pcb)
        assert result["success"] is False

    def test_error_contains_stderr(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        fake_proc = _make_subprocess_result(returncode=2, stderr="fatal kicad error xyz")
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", return_value=fake_proc):
            result = _run(pcb)
        assert "fatal kicad error xyz" in result["error"]


# ---------------------------------------------------------------------------
# Test: output file missing after successful subprocess
# ---------------------------------------------------------------------------

class TestOutputFileMissing:

    def test_missing_output_file_returns_failure(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        # returncode=0 but we never write the output file
        fake_proc = _make_subprocess_result(returncode=0)
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", return_value=fake_proc):
            result = _run(pcb)
        assert result["success"] is False
        assert "not created" in result["error"].lower() or "DRC report file" in result["error"]


# ---------------------------------------------------------------------------
# Test: JSON parse failure
# ---------------------------------------------------------------------------

class TestJsonParseFail:

    def test_invalid_json_returns_failure(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")

        def _write_bad_json(output_file):
            def _side_effect(*args, **kwargs):
                with open(output_file, "w") as f:
                    f.write("this is not json {{{")
                return _make_subprocess_result(returncode=0)
            return _side_effect

        # We need to intercept the actual output_file path used by the function.
        # Patch subprocess.run with a side_effect that writes bad JSON into
        # whatever output_file the function computed.
        original_run = subprocess.run

        def _capturing_run(cmd, *args, **kwargs):
            output_file = cmd[cmd.index("--output") + 1]
            with open(output_file, "w") as f:
                f.write("this is not json {{{")
            return _make_subprocess_result(returncode=0)

        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", side_effect=_capturing_run):
            result = _run(pcb)

        assert result["success"] is False
        assert "JSON" in result["error"]


# ---------------------------------------------------------------------------
# Shared helper: write valid DRC JSON via subprocess side_effect
# ---------------------------------------------------------------------------

def _patched_run_writing(drc_data):
    """Return a subprocess.run side_effect that writes drc_data as JSON."""
    def _side_effect(cmd, *args, **kwargs):
        output_file = cmd[cmd.index("--output") + 1]
        with open(output_file, "w") as f:
            json.dump(drc_data, f)
        return _make_subprocess_result(returncode=0)
    return _side_effect


# ---------------------------------------------------------------------------
# Test: success path — empty violations
# ---------------------------------------------------------------------------

class TestSuccessEmpty:

    def test_empty_violations(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", side_effect=_patched_run_writing({"violations": []})):
            result = _run(pcb)
        assert result["success"] is True
        assert result["method"] == "cli"
        assert result["total_violations"] == 0
        assert result["violation_categories"] == {}
        assert result["violations"] == []


# ---------------------------------------------------------------------------
# Test: success path — multiple violations
# ---------------------------------------------------------------------------

class TestSuccessMultipleViolations:

    def test_counts_and_categorizes(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        drc_data = {
            "violations": [
                {"rule_id": "clearance", "message": "Too close"},
                {"rule_id": "clearance", "message": "Too close again"},
                {"rule_id": "track_width", "message": "Track too narrow"},
            ]
        }
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", side_effect=_patched_run_writing(drc_data)):
            result = _run(pcb)
        assert result["success"] is True
        assert result["total_violations"] == 3
        assert result["violation_categories"] == {"clearance": 2, "track_width": 1}


# ---------------------------------------------------------------------------
# Test: categorization fallbacks
# ---------------------------------------------------------------------------

class TestCategorizationFallbacks:

    def _run_with_violations(self, tmp_path, violations):
        pcb = str(tmp_path / "board.kicad_pcb")
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", side_effect=_patched_run_writing({"violations": violations})):
            return _run(pcb)

    def test_falls_back_to_type_when_no_rule_id(self, tmp_path):
        violations = [{"type": "silk_overlap", "message": "Silkscreen overlaps"}]
        result = self._run_with_violations(tmp_path, violations)
        assert result["violation_categories"] == {"silk_overlap": 1}

    def test_falls_back_to_message_when_no_rule_id_or_type(self, tmp_path):
        violations = [{"message": "Pad too small"}]
        result = self._run_with_violations(tmp_path, violations)
        assert result["violation_categories"] == {"Pad too small": 1}

    def test_falls_back_to_unknown_when_all_missing(self, tmp_path):
        violations = [{}]
        result = self._run_with_violations(tmp_path, violations)
        assert result["violation_categories"] == {"Unknown": 1}

    def test_rule_id_wins_over_type(self, tmp_path):
        """rule_id takes priority over type (stable key preferred)."""
        violations = [{"rule_id": "clearance", "type": "some_type", "message": "msg"}]
        result = self._run_with_violations(tmp_path, violations)
        assert "clearance" in result["violation_categories"]
        assert "some_type" not in result["violation_categories"]


# ---------------------------------------------------------------------------
# Test: external-interface drift — unknown top-level key
# ---------------------------------------------------------------------------

class TestExternalInterfaceDrift:

    def test_unknown_top_level_key_silently_returns_zero_violations(self, tmp_path):
        """
        Documents the open 'External interface verification' gap.

        If a future KiCad version changes the top-level JSON key from
        'violations' to something else (e.g. 'results'), the current
        implementation silently falls back to an empty list via
        drc_report.get('violations', []).

        This test PINS that silent-zero behaviour so any future tightening
        of the schema contract is visible.
        """
        pcb = str(tmp_path / "board.kicad_pcb")
        future_format = {
            "results": [  # hypothetical KiCad future key
                {"rule_id": "clearance", "message": "Too close"},
            ]
        }
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", side_effect=_patched_run_writing(future_format)):
            result = _run(pcb)
        # Current behaviour: silently succeeds with zero violations
        assert result["success"] is True
        assert result["total_violations"] == 0
        assert result["violations"] == []
        # NOTE: this is a known gap — a missing 'violations' key should ideally
        # warn or fail rather than silently return zero. See project_external_interface_verification.md


# ---------------------------------------------------------------------------
# Test: parse_drc_report — pure field extraction (no subprocess / KiCad)
# ---------------------------------------------------------------------------

class TestParseDrcReport:
    """The DRC report parse, extracted from run_drc_via_cli so it is testable
    in-process. Pins the h-drc-arrays fix: ALL THREE kicad-cli arrays are
    captured (the old code read only 'violations')."""

    def test_captures_all_three_arrays(self):
        from kicad_mcp.tools.drc_impl.cli_drc import parse_drc_report
        report = {
            "violations": [{"rule_id": "clearance"}, {"rule_id": "clearance"}],
            "unconnected_items": [{"code": "x"}, {"code": "y"}, {"code": "z"}],
            "schematic_parity": [{"code": "p"}],
        }
        out = parse_drc_report(report)
        assert out["total_violations"] == 6            # 2 + 3 + 1, NOT 2 (the old bug)
        assert out["unconnected_count"] == 3
        assert out["parity_count"] == 1
        assert out["unconnected_items"] == report["unconnected_items"]
        assert out["schematic_parity"] == report["schematic_parity"]
        assert out["violation_categories"] == {
            "clearance": 2, "unconnected": 3, "schematic_parity": 1,
        }

    def test_board_with_only_unconnected_is_not_clean(self):
        """The exact h-drc-arrays scenario: 0 clearance violations but 12 unrouted
        nets must NOT report a clean board (it did before the fix)."""
        from kicad_mcp.tools.drc_impl.cli_drc import parse_drc_report
        out = parse_drc_report({"violations": [], "unconnected_items": [{"a": 1}] * 12})
        assert out["total_violations"] == 12
        assert out["violations"] == []

    def test_clean_board_is_zero(self):
        from kicad_mcp.tools.drc_impl.cli_drc import parse_drc_report
        out = parse_drc_report({"violations": []})
        assert out["total_violations"] == 0
        assert out["violation_categories"] == {}
        assert out["unconnected_count"] == 0 and out["parity_count"] == 0

    def test_rule_id_fallback_chain(self):
        from kicad_mcp.tools.drc_impl.cli_drc import parse_drc_report
        out = parse_drc_report({"violations": [
            {"type": "silk_overlap"},          # no rule_id -> type
            {"message": "Pad too small"},      # no rule_id/type -> message
            {},                                # nothing -> Unknown
        ]})
        assert out["violation_categories"] == {
            "silk_overlap": 1, "Pad too small": 1, "Unknown": 1,
        }


# ---------------------------------------------------------------------------
# Test: subprocess exception
# ---------------------------------------------------------------------------

class TestSubprocessException:

    @pytest.mark.parametrize("exc", [
        OSError("kicad-cli: No such file or directory"),
        subprocess.TimeoutExpired(cmd="kicad-cli", timeout=60),
        subprocess.SubprocessError("generic subprocess failure"),
    ])
    def test_exception_returns_failure_not_raise(self, tmp_path, exc):
        pcb = str(tmp_path / "board.kicad_pcb")
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", side_effect=exc):
            result = _run(pcb)
        assert result["success"] is False
        assert "Error in CLI DRC" in result["error"]

    def test_exception_error_contains_exception_text(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", side_effect=OSError("disk full")):
            result = _run(pcb)
        assert "disk full" in result["error"]


# ---------------------------------------------------------------------------
# Test: progress reporting
# ---------------------------------------------------------------------------

class TestProgressReporting:

    def _make_ctx(self):
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        ctx.info = AsyncMock()
        return ctx

    def test_ctx_methods_called_on_success(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        ctx = self._make_ctx()
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", side_effect=_patched_run_writing({"violations": []})):
            asyncio.run(run_drc_via_cli(pcb, ctx))
        assert ctx.report_progress.call_count >= 1
        assert ctx.info.call_count >= 1

    def test_ctx_none_does_not_raise(self, tmp_path):
        """ctx=None must not cause AttributeError."""
        pcb = str(tmp_path / "board.kicad_pcb")
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", side_effect=_patched_run_writing({"violations": []})):
            result = _run(pcb, ctx=None)
        assert result["success"] is True

    def test_progress_reported_at_multiple_stages(self, tmp_path):
        """At least three progress calls: before CLI, after DRC, after parse."""
        pcb = str(tmp_path / "board.kicad_pcb")
        ctx = self._make_ctx()
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", side_effect=_patched_run_writing({"violations": []})):
            asyncio.run(run_drc_via_cli(pcb, ctx))
        # Three report_progress calls in the source: 50, 70, 90
        assert ctx.report_progress.call_count == 3

    def test_ctx_info_called_with_violation_count(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        ctx = self._make_ctx()
        drc_data = {"violations": [{"rule_id": "clearance", "message": "close"}]}
        with patch(f"{_MODULE}.get_kicad_cli_path", return_value="/fake/kicad-cli"), \
             patch("subprocess.run", side_effect=_patched_run_writing(drc_data)):
            asyncio.run(run_drc_via_cli(pcb, ctx))
        info_calls = [str(call) for call in ctx.info.call_args_list]
        assert any("1" in c for c in info_calls)
