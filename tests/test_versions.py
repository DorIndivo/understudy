"""Interpretation history: runs accumulate, and nothing is overwritten."""

from __future__ import annotations

import json

import pytest

from understudy.interpret import versions
from understudy.interpret.schema import Interpretation


@pytest.fixture
def recording(tmp_path):
    return tmp_path


def make(sop="# SOP\n", name="A process", steps=2):
    return Interpretation.model_validate({
        "process_name": name,
        "goal": "g",
        "applications": [],
        "preconditions": [],
        "variables": [],
        "procedure": [
            {"number": i + 1, "action": "click", "intent": f"step {i + 1}",
             "target": {"description": f"the {i + 1}th control"},
             "source_steps": [i], "confidence": "high"}
            for i in range(steps)
        ],
        "completion_criteria": "done",
        "confidence": "high",
        "gaps": [],
        "sop": sop,
    })


class TestSaving:
    def test_the_first_run_is_001(self, recording):
        version = versions.save(recording, make(), {"effort": "high"})
        assert version.number == 1
        assert version.path == recording / "interpretations" / "001"
        assert (version.path / "sop.md").exists()
        assert (version.path / "procedure.json").exists()
        assert (version.path / "run.json").exists()

    def test_runs_accumulate_without_overwriting(self, recording):
        versions.save(recording, make(sop="first", name="One"), {})
        versions.save(recording, make(sop="second", name="Two"), {})
        saved = versions.list_versions(recording)
        assert [v.number for v in saved] == [1, 2]
        # The point of the feature: run 001 still says what it said.
        assert saved[0].load().sop == "first"
        assert saved[1].load().sop == "second"

    def test_the_newest_run_is_mirrored_at_the_recording_root(self, recording):
        versions.save(recording, make(sop="old"), {})
        versions.save(recording, make(sop="new"), {})
        assert (recording / "sop.md").read_text() == "new"
        assert json.loads((recording / "procedure.json").read_text())["procedure"]

    def test_metadata_records_how_the_run_was_made(self, recording):
        version = versions.save(recording, make(), {"effort": "max", "cost_usd": 0.42})
        meta = json.loads((version.path / "run.json").read_text())
        assert meta["effort"] == "max" and meta["cost_usd"] == 0.42
        assert meta["number"] == 1 and meta["created"]

    def test_sop_is_not_duplicated_into_the_procedure_file(self, recording):
        version = versions.save(recording, make(sop="body"), {})
        assert "sop" not in json.loads((version.path / "procedure.json").read_text())


class TestReading:
    def test_no_runs_yet_is_an_empty_list_not_an_error(self, recording):
        assert versions.list_versions(recording) == []

    def test_find_by_number(self, recording):
        versions.save(recording, make(), {})
        versions.save(recording, make(), {})
        assert versions.find(recording, 2).number == 2
        assert versions.find(recording, 7) is None

    def test_unrelated_directories_are_ignored(self, recording):
        versions.save(recording, make(), {})
        (recording / "interpretations" / "scratch").mkdir()
        assert [v.number for v in versions.list_versions(recording)] == [1]

    def test_a_run_with_unreadable_metadata_still_lists(self, recording):
        # Losing run.json should cost you the metadata, not the run.
        version = versions.save(recording, make(sop="kept"), {})
        (version.path / "run.json").write_text("{ not json")
        listed = versions.list_versions(recording)
        assert len(listed) == 1 and listed[0].load().sop == "kept"
