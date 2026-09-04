"""Orchestrates the three capture channels into one recording directory.

Channel responsibilities and where their work happens:

  event tap thread   receives input, resolves the accessibility element (bounded
                     by ax.MESSAGING_TIMEOUT_S), takes the 'before' frame from
                     the keyframe ring buffer, appends the raw event
  keyframe thread    grabs each display every KEYFRAME_INTERVAL_S, keeping the
                     ring buffer warm so 'before' frames need no time travel
  post-frame thread  grabs the 'after' frame POST_CLICK_DELAY_S past each click
  frame writer       encodes and writes WebP off all of the above

Nothing but the event tap runs on the tap thread, so encoding or disk latency can
never cause macOS to disable the tap.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from understudy.capture import ax
from understudy.capture.clock import Clock
from understudy.capture.events import EventTap, TapEvent
from understudy.capture.frames import (
    POST_CLICK_DELAY_S,
    Display,
    FrameWriter,
    display_for,
    enumerate_displays,
)
from understudy.permissions import check_all
from understudy.trace.models import AppContext, Element, Manifest, RawEvent

log = logging.getLogger(__name__)

KEYFRAME_INTERVAL_S = 0.5
FOCUS_CACHE_MS = 400

# Many window titles mutate continuously without the window changing: spinners,
# unread badges, elapsed timers, a leading dot for unsaved changes. Comparing raw
# titles would emit a context_switch on every keyframe, so titles are compared
# through this normalizer instead.
_TITLE_NOISE = re.compile(
    r"^[^\w(\[]+|"          # leading spinner / bullet / status glyph
    r"\s*\(\d+\)\s*|"      # (3) unread counters
    r"\s*[\u2022\u25cf]\s*|"  # bullet dot marking unsaved state
    r"\s*\d+:\d{2}(:\d{2})?\s*"  # elapsed timers
)


def _title_key(title: str | None) -> str:
    """A comparison key for a window title, stripped of live-updating decoration."""
    if not title:
        return ""
    return _TITLE_NOISE.sub("", title).strip().casefold()


class RecordingSession:
    def __init__(
        self,
        out_dir: Path,
        duration_s: float,
        redaction_patterns: list[str] | None = None,
        goal: str | None = None,
    ):
        self.out_dir = out_dir
        self.duration_s = duration_s
        self.goal = goal
        self.redaction_patterns = redaction_patterns or []
        self.clock = Clock()
        self.displays: list[Display] = []
        self.frames: FrameWriter | None = None
        self._events_file = None
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._capture_index = 0
        self._last_app: AppContext | None = None
        self._notes: list[str] = []
        self._focus_cache: tuple[int, Element] | None = None

    # -- lifecycle ---------------------------------------------------------------

    def run(self) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.displays = enumerate_displays()
        self.frames = FrameWriter(self.out_dir / "frames")
        started_at = datetime.now(timezone.utc).isoformat()

        # Prime the ring buffer so the very first click has a 'before' frame.
        for display in self.displays:
            self.frames.grab(display)

        self._events_file = (self.out_dir / "events.jsonl").open("w", encoding="utf-8")
        tap = EventTap(self._on_event)
        keyframes = threading.Thread(target=self._keyframe_loop, name="keyframes", daemon=True)

        try:
            tap.start()
            keyframes.start()
            self._record_app_context(force=True)
            self._stop.wait(self.duration_s)
        finally:
            self._stop.set()
            tap.stop()
            keyframes.join(timeout=2)
            duration = self.clock.elapsed_s()
            if self.frames is not None:
                self.frames.close()
            if self._events_file is not None:
                self._events_file.close()

        self._write_manifest(started_at, duration)
        return self.out_dir

    def _write_manifest(self, started_at: str, duration: float) -> None:
        manifest = Manifest(
            recording_id=self.out_dir.name,
            started_at=started_at,
            goal=self.goal,
            duration_s=round(duration, 3),
            clock_origin_ns=self.clock.origin_ns,
            displays=[d.to_info() for d in self.displays],
            permissions={p.name: p.granted for p in check_all()},
            redaction_patterns=self.redaction_patterns,
            notes=self._notes,
        )
        (self.out_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2))

    # -- channels ----------------------------------------------------------------

    def _keyframe_loop(self) -> None:
        while not self._stop.wait(KEYFRAME_INTERVAL_S):
            for display in self.displays:
                image = self.frames.grab(display)
                if image is None:
                    continue
            self._record_app_context()

    def _record_app_context(self, force: bool = False) -> None:
        """Emit a context_switch event when the frontmost app changes."""
        try:
            current = ax.frontmost_app()
        except Exception:
            return
        changed = self._last_app is None or (
            current.bundle_id != self._last_app.bundle_id
            or _title_key(current.window_title) != _title_key(self._last_app.window_title)
            or current.url != self._last_app.url
        )
        if not (force or changed):
            return
        self._last_app = current
        self._append(RawEvent(t_ms=self.clock.now_ms(), kind="app_change", app=current))

    def _on_event(self, event: TapEvent) -> None:
        """Runs on the tap thread -- must stay fast."""
        t_ms = self.clock.now_ms()
        raw = RawEvent(
            t_ms=t_ms,
            kind=event.kind,
            x=event.x,
            y=event.y,
            button=event.button,
            click_count=event.click_count,
            key_code=event.key_code,
            modifiers=event.modifiers,
            scroll_dy=event.scroll_dy,
            scroll_dx=event.scroll_dx,
        )

        if event.kind == "key_down":
            # Resolve the focused element *before* buffering any character: it
            # decides whether this is a password field (drop the characters) and
            # it names the field the text is going into.
            focused = self._focused_element(t_ms)
            raw.element = focused if focused.resolved else None
            raw.chars = None if focused.secure else event.chars
            self._append(raw)
            return

        if event.kind in ("mouse_down", "scroll") and event.x is not None:
            # One accessibility lookup answers both "what was clicked" and "which
            # app owns it"; asking separately would double the work on this thread.
            raw.element, raw.app = self._safe(
                lambda: ax.element_and_app_at(event.x, event.y),
                default=(Element(), AppContext()),
            )
            if event.kind == "mouse_down":
                self._focus_cache = None   # a click may move focus to another field
                self._capture_frames(raw, event)

        self._append(raw)

    def _focused_element(self, t_ms: int) -> Element:
        """Focused element, cached briefly.

        Fast typing produces events every few milliseconds; re-walking the
        accessibility tree for each one would risk macOS disabling the tap for
        being slow. Focus rarely changes mid-word, so a short cache is safe --
        and it is deliberately short so that tabbing to a new field, including a
        password field, is picked up immediately.
        """
        if self._focus_cache is not None and t_ms - self._focus_cache[0] < FOCUS_CACHE_MS:
            return self._focus_cache[1]
        element = self._safe(ax.focused_element, default=Element())
        self._focus_cache = (t_ms, element)
        return element

    def _capture_frames(self, raw: RawEvent, event: TapEvent) -> None:
        """Attach before/crop frames now and schedule the after frame."""
        display = display_for(event.x, event.y, self.displays)
        if display is None or self.frames is None:
            return
        index = self._capture_index
        self._capture_index += 1

        before = self.frames.latest(display)
        pre = self.frames.save(before, f"{index:03d}_pre")
        crop = self.frames.save_crop(before, f"{index:03d}_crop", event.x, event.y, display)
        raw.frame_path = pre
        raw.extra["crop_path"] = crop
        raw.extra["post_path"] = f"frames/{index:03d}_post.webp"
        raw.extra["capture_index"] = index

        timer = threading.Timer(
            POST_CLICK_DELAY_S, self._grab_post, args=(display, index)
        )
        timer.daemon = True
        timer.start()

    def _grab_post(self, display: Display, index: int) -> None:
        if self.frames is None or self._stop.is_set():
            return
        image = self.frames.grab(display)
        self.frames.save(image, f"{index:03d}_post")

    # -- helpers -----------------------------------------------------------------

    @staticmethod
    def _safe(fn, default):
        try:
            return fn()
        except Exception as exc:
            log.debug("capture helper failed: %s", exc)
            return default

    def _append(self, event: RawEvent) -> None:
        line = event.model_dump_json(exclude_none=True)
        with self._write_lock:
            if self._events_file is None:
                return
            self._events_file.write(line + "\n")
            self._events_file.flush()
