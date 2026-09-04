"""Low-level input capture via a Quartz CGEventTap.

The tap is created **listen-only**, so this process observes events without
being able to modify or delay them -- a recorder should never be able to break
the input it is watching.

Threading contract: the tap runs its own CFRunLoop on a dedicated thread and
invokes `handler` synchronously for each event. The handler therefore runs on the
tap thread and must stay fast (single-digit ms). Accessibility lookups are
acceptable there because they are bounded by `ax.MESSAGING_TIMEOUT_S`; screen
grabs and disk writes are not, and must be queued elsewhere.

macOS silently disables a tap whose callback runs long (`kCGEventTapDisabledByTimeout`),
which would end the recording without an error -- `_callback` detects that and
re-enables the tap.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import Quartz

log = logging.getLogger(__name__)

_MODIFIER_FLAGS = (
    (Quartz.kCGEventFlagMaskCommand, "Cmd"),
    (Quartz.kCGEventFlagMaskShift, "Shift"),
    (Quartz.kCGEventFlagMaskAlternate, "Option"),
    (Quartz.kCGEventFlagMaskControl, "Control"),
    (Quartz.kCGEventFlagMaskSecondaryFn, "Fn"),
)

_EVENT_KINDS = {
    Quartz.kCGEventLeftMouseDown: ("mouse_down", "left"),
    Quartz.kCGEventLeftMouseUp: ("mouse_up", "left"),
    Quartz.kCGEventRightMouseDown: ("mouse_down", "right"),
    Quartz.kCGEventRightMouseUp: ("mouse_up", "right"),
    Quartz.kCGEventLeftMouseDragged: ("mouse_drag", "left"),
    Quartz.kCGEventKeyDown: ("key_down", None),
    Quartz.kCGEventScrollWheel: ("scroll", None),
}

_EVENT_MASK = sum(Quartz.CGEventMaskBit(t) for t in _EVENT_KINDS)


@dataclass
class TapEvent:
    """A raw event, before any accessibility or visual enrichment."""

    kind: str
    x: float | None = None
    y: float | None = None
    button: str | None = None
    click_count: int | None = None
    key_code: int | None = None
    chars: str | None = None
    modifiers: list[str] = field(default_factory=list)
    scroll_dy: float | None = None
    scroll_dx: float | None = None


class TapCreationError(RuntimeError):
    """Raised when macOS refuses the tap -- almost always missing Input Monitoring."""


def _modifiers(flags: int) -> list[str]:
    return [name for mask, name in _MODIFIER_FLAGS if flags & mask]


def _unicode_chars(event) -> str | None:
    try:
        length, chars = Quartz.CGEventKeyboardGetUnicodeString(event, 16, None, None)
    except Exception:
        return None
    if not length or not chars:
        return None
    text = "".join(chars[:length]) if not isinstance(chars, str) else chars[:length]
    # Control characters (Return, Tab, Escape) carry no text meaning; the key
    # code identifies them instead.
    return text if text and text.isprintable() else None


class EventTap:
    """Observes global input events and delivers them to `handler`."""

    def __init__(self, handler: Callable[[TapEvent], None]):
        self._handler = handler
        self._tap = None
        self._runloop = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._error: Exception | None = None

    def _callback(self, proxy, event_type, event, refcon):
        # A disabled tap delivers no further events; re-enable and carry on.
        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            log.warning("event tap was disabled (type %s); re-enabling", event_type)
            Quartz.CGEventTapEnable(self._tap, True)
            return event

        entry = _EVENT_KINDS.get(event_type)
        if entry is None:
            return event
        kind, button = entry

        try:
            self._handler(self._build(event, event_type, kind, button))
        except Exception:
            # Never let a handler error kill the tap mid-recording.
            log.exception("event handler raised; continuing")
        return event

    def _build(self, event, event_type, kind: str, button: str | None) -> TapEvent:
        location = Quartz.CGEventGetLocation(event)
        tap_event = TapEvent(
            kind=kind,
            x=float(location.x),
            y=float(location.y),
            button=button,
            modifiers=_modifiers(Quartz.CGEventGetFlags(event)),
        )
        if kind in ("mouse_down", "mouse_up"):
            tap_event.click_count = int(
                Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventClickState)
            )
        elif kind == "key_down":
            tap_event.key_code = int(
                Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            )
            tap_event.chars = _unicode_chars(event)
        elif kind == "scroll":
            tap_event.scroll_dy = float(
                Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventDeltaAxis1)
            )
            tap_event.scroll_dx = float(
                Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventDeltaAxis2)
            )
        return tap_event

    def _run(self) -> None:
        try:
            self._tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,   # observe only; never modify input
                _EVENT_MASK,
                self._callback,
                None,
            )
            if self._tap is None:
                raise TapCreationError(
                    "macOS refused to create the event tap. Enable this terminal in "
                    "System Settings > Privacy & Security > Input Monitoring, then restart it."
                )
            source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._runloop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(self._runloop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(self._tap, True)
        except Exception as exc:
            self._error = exc
            self._started.set()
            return

        self._started.set()
        Quartz.CFRunLoopRun()

    def start(self, timeout: float = 5.0) -> None:
        """Start the tap thread, raising if the tap could not be created."""
        self._thread = threading.Thread(target=self._run, name="event-tap", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            raise TapCreationError("event tap did not start within %.1fs" % timeout)
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        if self._tap is not None:
            Quartz.CGEventTapEnable(self._tap, False)
        if self._runloop is not None:
            Quartz.CFRunLoopStop(self._runloop)
        if self._thread is not None:
            self._thread.join(timeout=5)
