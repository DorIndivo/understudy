"""Every command runs far enough to prove its module-level names resolve.

A refactor removed an import that only `record` used. The whole suite stayed
green because nothing here invoked a command -- the failure needed a real
recording to surface, which is exactly the kind of thing a test should catch
first. These are cheap: they never call the API and never capture a screen, they
just get each command past its imports and into its own code.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from understudy.cli import app
from understudy.trace.models import Manifest

runner = CliRunner()


@pytest.fixture
def recording(tmp_path):
    """A minimal but complete recording directory."""
    directory = tmp_path / "2026-01-01T00-00-00"
    directory.mkdir()
    manifest = Manifest(
        recording_id="2026-01-01T00-00-00",
        started_at="2026-01-01T00:00:00+00:00",
        duration_s=1.0,
        clock_origin_ns=0,
        goal="do the thing",
    )
    (directory / "manifest.json").write_text(manifest.model_dump_json())
    (directory / "events.jsonl").write_text(
        json.dumps({"t_ms": 0, "kind": "mouse_down", "x": 1, "y": 2}) + "\n"
        + json.dumps({"t_ms": 40, "kind": "mouse_up", "x": 1, "y": 2}) + "\n"
    )
    return directory


class TestCommandsResolve:
    """The regression these exist for: a name missing from a command's module."""

    def test_every_command_is_listed(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for command in ("doctor", "record", "inspect", "interpret", "synthesize"):
            assert command in result.output

    @pytest.mark.parametrize(
        "command", ["doctor", "record", "inspect", "interpret", "synthesize"]
    )
    def test_help_renders_for_each_command(self, command):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0

    def test_record_resolves_its_names_before_capturing(self, monkeypatch):
        """`record` referenced a name its module no longer imported.

        Stop at the permission gate: that is past every module-level lookup in
        the command body's argument defaults, without touching the screen.
        """
        monkeypatch.setattr("understudy.cli.check_all", lambda prompt=False: [])
        monkeypatch.setattr("understudy.cli.Redactor", _fail_after_names_resolve)
        result = runner.invoke(app, ["record", "-s", "1", "--countdown", "0", "-g", "x"])
        assert "NameError" not in str(result.output)
        assert not isinstance(result.exception, NameError)


class _Sentinel(Exception):
    pass


def _fail_after_names_resolve(*args, **kwargs):
    # Reached only once every name in `record` resolved; stops before capture.
    raise _Sentinel


class TestInspect:
    def test_inspect_normalizes_and_renders(self, recording):
        result = runner.invoke(app, ["inspect", str(recording)])
        assert result.exit_code == 0
        assert "do the thing" in result.output
        assert (recording / "steps.json").exists()

    def test_a_directory_that_is_not_a_recording_explains_itself(self, tmp_path):
        result = runner.invoke(app, ["inspect", str(tmp_path)])
        assert result.exit_code == 1
        assert "not a recording" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_an_interrupted_recording_says_so(self, tmp_path):
        # events.jsonl but no manifest: the shape a Ctrl-C leaves behind.
        (tmp_path / "events.jsonl").write_text("")
        result = runner.invoke(app, ["inspect", str(tmp_path)])
        assert "interrupted" in result.output


class TestAnalysisListing:
    def test_listing_with_no_analyses_is_not_a_crash(self, recording):
        result = runner.invoke(app, ["interpret", str(recording), "--list"])
        assert result.exit_code == 1
        assert "No analyses saved yet" in result.output

    def test_showing_a_missing_analysis_is_not_a_crash(self, recording):
        result = runner.invoke(app, ["interpret", str(recording), "--show", "9"])
        assert result.exit_code == 1
        assert "No analysis 009" in result.output

    def test_synthesize_refuses_a_single_recording(self, recording):
        result = runner.invoke(app, ["synthesize", str(recording)])
        assert result.exit_code != 0
        assert "at least two recordings" in result.output
