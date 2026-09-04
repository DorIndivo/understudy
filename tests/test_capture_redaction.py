"""The capture-time guarantee: password keystrokes never reach the events file.

`normalize` also drops secure text, but that is the second line of defence. The
promise the tool makes is that the characters are never written at all, so the
test has to observe what `_on_event` actually appends -- not what a later stage
does with it.
"""

from __future__ import annotations

import json

import pytest

from understudy.capture.events import TapEvent
from understudy.capture.session import RecordingSession
from understudy.trace.models import Element


@pytest.fixture
def session(tmp_path, monkeypatch):
    s = RecordingSession(out_dir=tmp_path, duration_s=1)
    written: list[dict] = []
    monkeypatch.setattr(s, "_append", lambda event: written.append(json.loads(event.model_dump_json())))
    s.written = written
    return s


def key_event(chars: str) -> TapEvent:
    return TapEvent(kind="key_down", chars=chars, key_code=1, modifiers=[])


class TestSecureFields:
    def test_characters_typed_into_a_password_field_are_not_written(self, session, monkeypatch):
        monkeypatch.setattr(
            session, "_focused_element",
            lambda t: Element(resolved=True, secure=True, role="AXSecureTextField"),
        )
        session._on_event(key_event("hunter2"))

        assert len(session.written) == 1
        record = session.written[0]
        assert record.get("chars") is None
        # The whole serialized event, not just the field: nothing may carry it.
        assert "hunter2" not in json.dumps(record)

    def test_characters_typed_into_an_ordinary_field_are_kept(self, session, monkeypatch):
        monkeypatch.setattr(
            session, "_focused_element",
            lambda t: Element(resolved=True, secure=False, role="AXTextField"),
        )
        session._on_event(key_event("INV-2024-113"))
        assert session.written[0]["chars"] == "INV-2024-113"

    def test_an_unresolvable_focus_still_records_the_keystroke(self, session, monkeypatch):
        # Failing closed on text would silently lose the typed values that make
        # a procedure reusable; AX enriches, it never gates.
        monkeypatch.setattr(session, "_focused_element", lambda t: Element())
        session._on_event(key_event("abc"))
        assert session.written[0]["chars"] == "abc"
        assert session.written[0].get("element") is None


class TestSecureInputMarker:
    """macOS secure input mode hides the keystrokes; the step must survive anyway.

    Verified against a real Chrome password field: focusing one produced zero
    `key_down` events at the tap, so the only evidence a credential was required
    is the click that landed on the secure element.
    """

    @staticmethod
    def _steps(secure: bool):
        from understudy.trace.models import Action, Element, RawEvent
        from understudy.trace.normalize import normalize

        element = Element(resolved=True, role="AXTextField",
                          subrole="AXSecureTextField" if secure else None,
                          title="Approver passcode", secure=secure)
        events = [
            RawEvent(t_ms=100, kind="mouse_down", x=10, y=20, element=element),
            RawEvent(t_ms=140, kind="mouse_up", x=10, y=20),
        ]
        return normalize(events), Action

    def test_a_click_into_a_password_field_emits_a_marker(self):
        steps, Action = self._steps(secure=True)
        assert [s.action for s in steps] == [Action.CLICK, Action.SECURE_INPUT]

    def test_an_ordinary_field_emits_no_marker(self):
        steps, Action = self._steps(secure=False)
        assert [s.action for s in steps] == [Action.CLICK]

    def test_the_marker_carries_the_field_but_never_a_value(self):
        steps, _ = self._steps(secure=True)
        marker = steps[-1]
        assert marker.target.title == "Approver passcode"
        assert marker.text is None
        assert "not observable" in marker.summary()
