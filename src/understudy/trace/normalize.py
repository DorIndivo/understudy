"""Turn raw input events into semantic steps.

This is the deterministic half of the pipeline: same events in, same steps out,
no model involved. Keeping it separate from capture is what makes it testable
from a fixture file, and keeping it separate from interpretation is what stops
the model from having to reason about mouse-down/mouse-up pairs.

The coalescing rules exist because raw events are far too granular to reason
about: forty key events are one act of typing an invoice number, and a burst of
scroll ticks is one act of scrolling down a page.
"""

from __future__ import annotations

import json
from pathlib import Path

from understudy.trace.models import (
    Action,
    AppContext,
    Element,
    Frames,
    RawEvent,
    Step,
)
from understudy.trace.redact import Redactor

CLICK_MAX_MS = 500        # mouse_down -> mouse_up beyond this is a drag, not a click
CLICK_MAX_PX = 5.0        # movement beyond this is a drag
SCROLL_GAP_MS = 300       # scroll ticks closer than this are one scroll
TYPING_GAP_MS = 2000      # keystrokes further apart than this are separate entries

# Non-printable keys worth recording by name; anything else printable is text.
KEY_NAMES = {
    36: "Return", 48: "Tab", 49: "Space", 51: "Delete", 53: "Escape",
    76: "Enter", 117: "ForwardDelete", 123: "Left", 124: "Right",
    125: "Down", 126: "Up", 115: "Home", 119: "End", 116: "PageUp", 121: "PageDown",
}


def load_events(path: Path) -> list[RawEvent]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(RawEvent.model_validate_json(line))
    return events


class _Builder:
    """Accumulates steps, tracking the app context and dwell time between them."""

    def __init__(self, redactor: Redactor):
        self.redactor = redactor
        self.steps: list[Step] = []
        self.app = AppContext()
        self._last_end_ms = 0

    def add(self, **kwargs) -> Step:
        t_ms = kwargs.pop("t_ms")
        step = Step(
            index=len(self.steps),
            t_ms=t_ms,
            dwell_ms=max(0, t_ms - self._last_end_ms),
            app=kwargs.pop("app", None) or self.app,
            **kwargs,
        )
        self._last_end_ms = t_ms
        self.steps.append(step)
        return step


def normalize(events: list[RawEvent], redactor: Redactor | None = None) -> list[Step]:
    redactor = redactor or Redactor()
    builder = _Builder(redactor)

    pending_down: RawEvent | None = None
    typing: list[RawEvent] = []
    scrolling: list[RawEvent] = []

    # Both buffers are cleared in place, never rebound: `_handle_key` holds a
    # reference to the list, and rebinding here would leave it appending to an
    # orphaned pre-flush buffer.
    def flush_typing() -> None:
        if not typing:
            return
        text = "".join(e.chars or "" for e in typing)
        secure = any((e.element.secure if e.element else False) for e in typing)
        if text or secure:
            first = typing[0]
            builder.add(
                t_ms=first.t_ms,
                action=Action.TYPE,
                target=_target_of(typing),
                # Secure keystrokes carry no characters, so the count of events
                # is the only length signal available.
                text=redactor.typed(text, secure, count=len(typing)),
                x=first.x,
                y=first.y,
            )
        typing.clear()

    def flush_scrolling() -> None:
        if not scrolling:
            return
        first = scrolling[0]
        builder.add(
            t_ms=first.t_ms,
            action=Action.SCROLL,
            target=first.element or Element(),
            scroll_dy=sum(e.scroll_dy or 0 for e in scrolling),
            x=first.x,
            y=first.y,
        )
        scrolling.clear()

    for event in events:
        # Any non-typing event ends an in-progress entry.
        if event.kind != "key_down" and typing:
            flush_typing()
        if event.kind != "scroll" and scrolling:
            flush_scrolling()

        match event.kind:
            case "app_change":
                if event.app is not None:
                    builder.app = event.app
                    builder.add(
                        t_ms=event.t_ms, action=Action.CONTEXT_SWITCH, app=event.app
                    )

            case "mouse_down":
                pending_down = event

            case "mouse_up":
                if pending_down is None:
                    continue
                _emit_mouse(builder, pending_down, event)
                pending_down = None

            case "scroll":
                if scrolling and event.t_ms - scrolling[-1].t_ms > SCROLL_GAP_MS:
                    flush_scrolling()
                scrolling.append(event)

            case "key_down":
                _handle_key(builder, event, typing, flush_typing)

    flush_typing()
    flush_scrolling()
    return builder.steps


def _handle_key(builder: _Builder, event: RawEvent, typing: list[RawEvent], flush) -> None:
    """Route a keystroke to either the typing buffer or its own `key` step."""
    shortcut_mods = [m for m in event.modifiers if m in ("Cmd", "Control", "Option")]
    named = KEY_NAMES.get(event.key_code or -1)

    # A shortcut or a named navigation key is an action in its own right, and
    # ends whatever was being typed.
    if shortcut_mods or (named and named != "Space"):
        flush()
        label = "+".join(shortcut_mods + [named or (event.chars or "").upper() or "?"])
        builder.add(
            t_ms=event.t_ms,
            action=Action.KEY,
            key=label,
            target=event.element or Element(),
            x=event.x,
            y=event.y,
        )
        return

    if event.chars is None and not (event.element and event.element.secure):
        return  # unprintable and unnamed -- nothing meaningful to record

    if typing and event.t_ms - typing[-1].t_ms > TYPING_GAP_MS:
        flush()
    typing.append(event)


def _emit_mouse(builder: _Builder, down: RawEvent, up: RawEvent) -> None:
    duration = up.t_ms - down.t_ms
    distance = max(abs((up.x or 0) - (down.x or 0)), abs((up.y or 0) - (down.y or 0)))
    frames = Frames(
        pre=down.frame_path,
        crop=down.extra.get("crop_path"),
        post=down.extra.get("post_path"),
    )

    if duration > CLICK_MAX_MS and distance > CLICK_MAX_PX:
        action = Action.DRAG
    elif down.button == "right":
        action = Action.RIGHT_CLICK
    elif (down.click_count or 1) >= 2:
        action = Action.DOUBLE_CLICK
    else:
        action = Action.CLICK

    target = down.element or Element()
    builder.add(
        t_ms=down.t_ms,
        action=action,
        target=target,
        app=down.app,
        x=down.x,
        y=down.y,
        frames=frames,
    )

    if action is Action.CLICK and target.secure:
        # The keystrokes that follow are invisible to us: macOS secure input
        # mode suppresses them before any tap sees them. Record that a secret
        # was required here, so the step survives into the procedure even though
        # its content never can.
        builder.add(t_ms=down.t_ms + 1, action=Action.SECURE_INPUT, target=target, app=down.app)


def _target_of(events: list[RawEvent]) -> Element:
    """Best element among buffered keystrokes -- the first that resolved."""
    for event in events:
        if event.element and event.element.resolved:
            return event.element
    return Element()


def write_steps(steps: list[Step], path: Path) -> None:
    path.write_text(
        json.dumps([s.model_dump(mode="json", exclude_none=True) for s in steps], indent=2)
    )
