"""The request assembly is testable without the API: structure, ordering, budget."""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from understudy.interpret.prompt import _plan_images, _step_facts, build_blocks
from understudy.trace.models import (
    Action, AppContext, DisplayInfo, Element, Frames, Manifest, Step, TypedText,
)


@pytest.fixture
def recording(tmp_path):
    """A two-click recording with real (tiny) image files on disk."""
    frames = tmp_path / "frames"
    frames.mkdir()
    for i in range(2):
        for kind in ("pre", "crop", "post"):
            Image.new("RGB", (40, 30), (i * 40, 80, 120)).save(
                frames / f"{i:03d}_{kind}.webp", "WEBP"
            )
    manifest = Manifest(
        recording_id="t", started_at="2026-09-02T10:00:00Z", duration_s=5.0,
        clock_origin_ns=0, displays=[DisplayInfo(index=1, width=3024, height=1964, scale=2.0)],
    )
    steps = [
        Step(
            index=i, t_ms=i * 1000, action=Action.CLICK,
            target=Element(resolved=True, role="AXButton", title=f"Button{i}"),
            app=AppContext(name="Chrome"),
            frames=Frames(pre=f"frames/{i:03d}_pre.webp", crop=f"frames/{i:03d}_crop.webp",
                          post=f"frames/{i:03d}_post.webp"),
            x=10, y=10,
        )
        for i in range(2)
    ]
    return tmp_path, manifest, steps


class TestStepFacts:
    def test_names_the_element_when_accessibility_resolved_it(self):
        step = Step(index=0, t_ms=0, action=Action.CLICK,
                    target=Element(resolved=True, role="AXButton", title="Approve"))
        assert "label 'Approve'" in _step_facts(step)

    def test_flags_an_unresolved_element_and_points_at_the_image(self):
        facts = _step_facts(Step(index=0, t_ms=0, action=Action.CLICK))
        assert "NOT RESOLVED" in facts and "close-up" in facts

    def test_reports_a_long_pause(self):
        step = Step(index=0, t_ms=5000, dwell_ms=4200, action=Action.CLICK)
        assert "paused 4.2s" in _step_facts(step)

    def test_omits_a_short_pause(self):
        assert "paused" not in _step_facts(Step(index=0, t_ms=1, dwell_ms=200, action=Action.CLICK))

    def test_redacted_text_shows_length_and_forbids_guessing(self):
        step = Step(index=0, t_ms=0, action=Action.TYPE,
                    text=TypedText(text=None, redacted=True, length=9))
        facts = _step_facts(step)
        assert "9 characters" in facts and "do not guess" in facts

    def test_disabled_control_is_called_out(self):
        step = Step(index=0, t_ms=0, action=Action.CLICK,
                    target=Element(resolved=True, role="AXButton", title="Send", enabled=False))
        assert "DISABLED" in _step_facts(step)


class TestBlocks:
    def test_every_image_is_immediately_preceded_by_a_caption(self, recording):
        tmp, manifest, steps = recording
        blocks = build_blocks(tmp, manifest, steps)
        for i, block in enumerate(blocks):
            if block["type"] == "image":
                assert blocks[i - 1]["type"] == "text"
                assert blocks[i - 1]["text"].endswith(":")

    def test_each_step_header_precedes_its_own_images(self, recording):
        tmp, manifest, steps = recording
        blocks = build_blocks(tmp, manifest, steps)
        current = None
        seen = []
        for block in blocks:
            if block["type"] == "text" and block["text"].lstrip().startswith("## Step "):
                current = block["text"].split("## Step ")[1].split("\n")[0]
            elif block["type"] == "image":
                seen.append(current)
        assert seen == ["0", "0", "0", "1", "1", "1"]

    def test_images_are_base64_webp(self, recording):
        tmp, manifest, steps = recording
        images = [b for b in build_blocks(tmp, manifest, steps) if b["type"] == "image"]
        assert len(images) == 6
        assert all(b["source"]["media_type"] == "image/webp" for b in images)
        assert all(b["source"]["type"] == "base64" for b in images)

    def test_missing_frame_file_is_skipped_not_fatal(self, recording):
        tmp, manifest, steps = recording
        (tmp / steps[0].frames.crop).unlink()
        images = [b for b in build_blocks(tmp, manifest, steps) if b["type"] == "image"]
        assert len(images) == 5

    def test_recording_with_no_frames_still_builds(self, recording):
        tmp, manifest, steps = recording
        for step in steps:
            step.frames = Frames()
        blocks = build_blocks(tmp, manifest, steps)
        assert not [b for b in blocks if b["type"] == "image"]
        assert any("Step 0" in b.get("text", "") for b in blocks)


class TestImageBudget:
    def _steps(self, n):
        return [
            Step(index=i, t_ms=i, action=Action.CLICK,
                 frames=Frames(pre=f"{i}p", crop=f"{i}c", post=f"{i}o"))
            for i in range(n)
        ]

    def test_budget_is_respected(self):
        assert len(_plan_images(self._steps(30), max_images=10)) == 10

    def test_crops_are_kept_before_anything_else(self):
        # The close-up identifies the target, so it is the last thing to drop.
        chosen = _plan_images(self._steps(10), max_images=10)
        assert chosen == {(i, "crop") for i in range(10)}

    def test_pre_frames_are_dropped_first(self):
        chosen = _plan_images(self._steps(10), max_images=25)
        assert sum(1 for _, k in chosen if k == "crop") == 10
        assert sum(1 for _, k in chosen if k == "post") == 10
        assert sum(1 for _, k in chosen if k == "pre") == 5

    def test_generous_budget_keeps_everything(self):
        assert len(_plan_images(self._steps(5), max_images=40)) == 15
