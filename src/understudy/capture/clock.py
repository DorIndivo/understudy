"""One monotonic time origin shared by every capture channel.

Events, frames and app-context samples are only useful if they can be placed on a
single timeline; wall-clock time is unsuitable because it can jump (NTP, sleep).
"""

from __future__ import annotations

import time


class Clock:
    def __init__(self) -> None:
        self.origin_ns = time.monotonic_ns()

    def now_ms(self) -> int:
        return (time.monotonic_ns() - self.origin_ns) // 1_000_000

    def elapsed_s(self) -> float:
        return (time.monotonic_ns() - self.origin_ns) / 1_000_000_000
