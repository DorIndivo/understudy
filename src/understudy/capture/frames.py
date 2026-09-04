"""Event-triggered screen capture.

Three images per click -- the screen just before, a close-up of the click point,
and the screen shortly after -- plus a periodic keyframe. Every image is written
with the index of the step it belongs to, so interpretation never has to guess
which action sits between two frames.

The one genuine hazard here is coordinate space: CGEvent reports mouse position
in *points*, while a screen capture may be in *pixels*. On a Retina display those
can differ by 2x, so a crop that gets the factor wrong lands nowhere near the
cursor -- and the factor is not knowable in advance: NSScreen reports a backing
scale of 2.0, but mss returns point-sized images on some macOS/mss combinations.
So the scale is *measured* from the first grab of each display
(`FrameWriter.capture_scale`) rather than assumed, and that measured value is the
single place point->pixel conversion happens.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path

import mss
from AppKit import NSScreen
from PIL import Image

from understudy.trace.models import DisplayInfo

log = logging.getLogger(__name__)

CROP_SIZE_PT = 320          # size of the cursor close-up, in points
FULL_MAX_WIDTH_PX = 1600    # downscale ceiling for full-screen frames on disk
WEBP_QUALITY = 80
POST_CLICK_DELAY_S = 0.4    # long enough for a menu to open or a row to repaint


@dataclass(frozen=True)
class Display:
    """One screen, in the point space that mouse events use."""

    index: int          # index into mss's monitor list (1-based; 0 is the union of all)
    x: float
    y: float
    width: float        # in points
    height: float
    scale: float        # 2.0 on Retina

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.width and self.y <= py < self.y + self.height

    def to_local(self, px: float, py: float) -> tuple[float, float]:
        """Global point coordinates -> point coordinates within this display."""
        return (px - self.x, py - self.y)

    def to_info(self) -> DisplayInfo:
        return DisplayInfo(
            index=self.index,
            width=int(self.width * self.scale),
            height=int(self.height * self.scale),
            scale=self.scale,
        )


def enumerate_displays() -> list[Display]:
    """Screens in top-left-origin point space, matching CGEvent coordinates.

    NSScreen uses a bottom-left origin, so y is flipped against the primary
    screen's height; mss monitors are already top-left, and the two lists are
    matched by position.
    """
    screens = NSScreen.screens()
    if not screens:
        return []
    primary_height = screens[0].frame().size.height

    displays: list[Display] = []
    with mss.mss() as sct:
        monitors = sct.monitors[1:]  # index 0 is the bounding box of all monitors
        for i, screen in enumerate(screens):
            frame = screen.frame()
            top_left_y = primary_height - frame.origin.y - frame.size.height
            scale = float(screen.backingScaleFactor())
            # Match this NSScreen to its mss monitor by top-left corner.
            monitor_index = i + 1
            for j, mon in enumerate(monitors, start=1):
                if abs(mon["left"] - frame.origin.x) < 2 and abs(mon["top"] - top_left_y) < 2:
                    monitor_index = j
                    break
            displays.append(
                Display(
                    index=monitor_index,
                    x=float(frame.origin.x),
                    y=float(top_left_y),
                    width=float(frame.size.width),
                    height=float(frame.size.height),
                    scale=scale,
                )
            )
    return displays


def display_for(px: float, py: float, displays: list[Display]) -> Display | None:
    for d in displays:
        if d.contains(px, py):
            return d
    return displays[0] if displays else None


class FrameWriter:
    """Grabs on the calling thread, encodes and writes on a worker thread.

    Grabbing must be synchronous (the pixels are only correct at that instant),
    but PNG/WebP encoding and disk I/O must not block the event tap, so encoding
    is queued.
    """

    def __init__(self, out_dir: Path, max_width: int = FULL_MAX_WIDTH_PX):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.max_width = max_width
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._worker, name="frame-writer", daemon=True)
        self._sct: mss.base.MSSBase | None = None
        self._latest: dict[int, Image.Image] = {}   # display index -> most recent keyframe
        self._capture_scale: dict[int, float] = {}  # measured, not assumed -- see module docstring
        self._lock = threading.Lock()
        self._thread.start()

    # -- capture (caller thread) -------------------------------------------------

    def _sct_instance(self):
        # mss is not thread-safe; this instance belongs to the capture thread.
        if self._sct is None:
            self._sct = mss.mss()
        return self._sct

    def grab(self, display: Display) -> Image.Image | None:
        """Capture one display. Returns a full-resolution PIL image."""
        try:
            shot = self._sct_instance().grab(self._sct_instance().monitors[display.index])
        except Exception as exc:
            log.warning("screen grab failed on display %s: %s", display.index, exc)
            return None
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        with self._lock:
            self._latest[display.index] = image
            if display.index not in self._capture_scale and display.width:
                self._capture_scale[display.index] = image.width / display.width
        return image

    def capture_scale(self, display: Display) -> float:
        """Pixels per point in this display's captures, measured from a real grab."""
        with self._lock:
            return self._capture_scale.get(display.index, 1.0)

    def latest(self, display: Display) -> Image.Image | None:
        """The most recent keyframe for a display -- serves as the 'before' frame.

        The keyframe cadence guarantees a recent image already exists, so the
        pre-click frame needs no time travel.
        """
        with self._lock:
            return self._latest.get(display.index)

    # -- writing (worker thread) -------------------------------------------------

    def save(self, image: Image.Image | None, name: str) -> str | None:
        """Queue an image for encoding. Returns the relative path it will have."""
        if image is None:
            return None
        self._queue.put((image, name, None))
        return f"frames/{name}.webp"

    def save_crop(
        self, image: Image.Image | None, name: str, px: float, py: float, display: Display
    ) -> str | None:
        """Queue a crop centred on a point given in *point* coordinates."""
        if image is None:
            return None
        scale = self.capture_scale(display)
        lx, ly = display.to_local(px, py)
        cx, cy = lx * scale, ly * scale
        half = (CROP_SIZE_PT * scale) / 2
        box = (int(cx - half), int(cy - half), int(cx + half), int(cy + half))
        self._queue.put((image, name, box))
        return f"frames/{name}.webp"

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            image, name, box = item
            try:
                self._write(image, name, box)
            except Exception as exc:
                log.warning("failed writing frame %s: %s", name, exc)
            finally:
                self._queue.task_done()

    def _write(self, image: Image.Image, name: str, box: tuple[int, int, int, int] | None) -> None:
        if box is not None:
            # Clamp to the image so a click near an edge still yields a valid crop.
            left = max(0, min(box[0], image.width - 1))
            top = max(0, min(box[1], image.height - 1))
            right = max(left + 1, min(box[2], image.width))
            bottom = max(top + 1, min(box[3], image.height))
            image = image.crop((left, top, right, bottom))
        elif image.width > self.max_width:
            ratio = self.max_width / image.width
            image = image.resize(
                (self.max_width, int(image.height * ratio)), Image.Resampling.LANCZOS
            )
        image.save(self.out_dir / f"{name}.webp", "WEBP", quality=WEBP_QUALITY)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=10)
        if self._sct is not None:
            self._sct.close()
            self._sct = None
