"""Normalization is deterministic, so it is tested from hand-written events."""

from __future__ import annotations

import pytest

from understudy.trace.models import Action, AppContext, Element, RawEvent
from understudy.trace.normalize import normalize
from understudy.trace.redact import Redactor


def down(t, x=100, y=100, clicks=1, button="left", element=None, app=None, index=0):
    return RawEvent(
        t_ms=t, kind="mouse_down", x=x, y=y, button=button, click_count=clicks,
        element=element, app=app,
        frame_path=f"frames/{index:03d}_pre.webp",
        extra={"crop_path": f"frames/{index:03d}_crop.webp",
               "post_path": f"frames/{index:03d}_post.webp"},
    )


def up(t, x=100, y=100, button="left"):
    return RawEvent(t_ms=t, kind="mouse_up", x=x, y=y, button=button)


def key(t, chars=None, code=0, mods=None, element=None):
    return RawEvent(t_ms=t, kind="key_down", chars=chars, key_code=code,
                    modifiers=mods or [], element=element)


class TestMouse:
    def test_down_up_pair_becomes_one_click(self):
        steps = normalize([down(1000), up(1080)])
        assert [s.action for s in steps] == [Action.CLICK]
        assert steps[0].t_ms == 1000

    def test_click_carries_its_three_frames(self):
        step = normalize([down(1000, index=7), up(1080)])[0]
        assert step.frames.pre == "frames/007_pre.webp"
        assert step.frames.crop == "frames/007_crop.webp"
        assert step.frames.post == "frames/007_post.webp"

    def test_click_count_two_is_a_double_click(self):
        assert normalize([down(0, clicks=2), up(60)])[0].action == Action.DOUBLE_CLICK

    def test_right_button_is_a_right_click(self):
        steps = normalize([down(0, button="right"), up(50, button="right")])
        assert steps[0].action == Action.RIGHT_CLICK

    def test_slow_distant_release_is_a_drag(self):
        steps = normalize([down(0, x=100, y=100), up(900, x=400, y=400)])
        assert steps[0].action == Action.DRAG

    def test_slow_but_stationary_release_is_still_a_click(self):
        # Holding the button down without moving is a click, not a drag.
        steps = normalize([down(0, x=100, y=100), up(900, x=101, y=100)])
        assert steps[0].action == Action.CLICK

    def test_unpaired_mouse_up_is_ignored(self):
        assert normalize([up(50)]) == []


class TestTyping:
    def test_keystroke_run_coalesces_into_one_step(self):
        events = [key(t, chars=c) for t, c in zip(range(0, 500, 100), "INV-24")]
        steps = normalize(events)
        assert len(steps) == 1
        assert steps[0].action == Action.TYPE
        assert steps[0].text.text == "INV-2"

    def test_long_pause_splits_two_entries(self):
        steps = normalize([key(0, "a"), key(50, "b"), key(9000, "c")])
        assert [s.text.text for s in steps] == ["ab", "c"]

    def test_shortcut_becomes_its_own_key_step_and_flushes_typing(self):
        steps = normalize([key(0, "h"), key(50, "i"), key(100, "s", code=1, mods=["Cmd"])])
        assert [s.action for s in steps] == [Action.TYPE, Action.KEY]
        assert steps[0].text.text == "hi"
        assert steps[1].key == "Cmd+S"

    def test_named_key_is_recorded_by_name(self):
        steps = normalize([key(0, code=48)])   # Tab
        assert steps[0].action == Action.KEY and steps[0].key == "Tab"

    def test_typing_targets_the_first_resolved_element(self):
        field = Element(resolved=True, role="AXTextField", title="Invoice ID")
        steps = normalize([key(0, "1", element=None), key(50, "2", element=field)])
        assert steps[0].target.title == "Invoice ID"

    def test_click_interrupts_typing(self):
        steps = normalize([key(0, "a"), down(100), up(150), key(300, "b")])
        assert [s.action for s in steps] == [Action.TYPE, Action.CLICK, Action.TYPE]


class TestScroll:
    def test_burst_coalesces_and_sums_delta(self):
        events = [RawEvent(t_ms=t, kind="scroll", x=1, y=1, scroll_dy=-3) for t in (0, 50, 100)]
        steps = normalize(events)
        assert len(steps) == 1
        assert steps[0].scroll_dy == -9

    def test_separated_bursts_stay_separate(self):
        events = [RawEvent(t_ms=t, kind="scroll", x=1, y=1, scroll_dy=-3) for t in (0, 50, 5000)]
        assert len(normalize(events)) == 2


class TestContext:
    def test_app_change_becomes_a_context_switch_and_sticks(self):
        chrome = AppContext(name="Chrome", bundle_id="com.google.Chrome")
        steps = normalize([RawEvent(t_ms=0, kind="app_change", app=chrome), down(100), up(150)])
        assert steps[0].action == Action.CONTEXT_SWITCH
        # Later steps inherit the app context even though the event carries none.
        assert steps[1].app.name == "Chrome"

    def test_dwell_is_the_gap_before_a_step(self):
        steps = normalize([down(0), up(50), down(3000), up(3050)])
        assert steps[0].dwell_ms == 0
        assert steps[1].dwell_ms == 3000


class TestDegradedAccessibility:
    """AX is additive: with nothing resolved, the trace must still be complete."""

    def test_steps_survive_with_no_ax_data_at_all(self):
        steps = normalize([down(0), up(50), key(200, "x")])
        assert [s.action for s in steps] == [Action.CLICK, Action.TYPE]
        assert steps[0].target.resolved is False
        assert steps[0].x == 100          # coordinates still carry the target
        assert steps[0].summary() == "click on unidentified element"


class TestRedaction:
    def test_secure_field_keystrokes_never_yield_text(self):
        secure = Element(resolved=True, secure=True)
        events = [key(t, chars=None, element=secure) for t in range(0, 400, 100)]
        step = normalize(events)[0]
        assert step.text.text is None
        assert step.text.redacted is True
        assert step.text.length == 4

    @pytest.mark.parametrize("secret", [
        "sk-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz1234",
        "AKIAIOSFODNN7EXAMPLE",
        "4111111111111111",
        "123-45-6789",
    ])
    def test_pattern_denylist_scrubs_secrets_typed_into_normal_fields(self, secret):
        events = [key(i * 10, chars=c) for i, c in enumerate(secret)]
        step = normalize(events)[0]
        assert secret not in (step.text.text or "")
        assert step.text.redacted is True

    def test_ordinary_text_is_left_alone(self):
        text = "INV-2024-113"
        events = [key(i * 10, chars=c) for i, c in enumerate(text)]
        step = normalize(events)[0]
        assert step.text.text == text
        assert step.text.redacted is False

    def test_secret_never_appears_anywhere_in_serialized_output(self):
        secret = "sk-abcdefghijklmnopqrstuvwx"
        events = [key(i * 10, chars=c) for i, c in enumerate(secret)]
        blob = normalize(events)[0].model_dump_json()
        assert "sk-abcdefghij" not in blob


class TestRedactor:
    def test_custom_patterns_replace_the_defaults(self):
        r = Redactor(patterns=[r"badger"])
        assert r.scrub("a badger here") == ("a [REDACTED] here", True)
        assert r.scrub("sk-abcdefghijklmnopqrstuvwx")[1] is False
