"""Preflight checks for the two macOS permissions this tool cannot work without.

Both fail *silently* rather than raising: without Accessibility the AX API returns
empty attributes for every element, and without Screen Recording `mss` returns
frames of the desktop wallpaper only. Checking up front is the difference between
a confusing empty trace and an actionable error message.
"""

from __future__ import annotations

from dataclasses import dataclass

import Quartz
from ApplicationServices import AXIsProcessTrustedWithOptions

# Defined in ApplicationServices but not always exported by name through pyobjc.
_AX_TRUSTED_CHECK_OPTION_PROMPT = "AXTrustedCheckOptionPrompt"

_SETTINGS_PANE = "System Settings > Privacy & Security > {section}"


@dataclass(frozen=True)
class Permission:
    name: str
    granted: bool
    how_to_fix: str


def check_accessibility(prompt: bool = False) -> Permission:
    """Whether this process may read the accessibility tree of other apps.

    With `prompt=True` macOS shows the "grant access" dialog once per binary.
    """
    granted = bool(
        AXIsProcessTrustedWithOptions({_AX_TRUSTED_CHECK_OPTION_PROMPT: prompt})
    )
    return Permission(
        name="Accessibility",
        granted=granted,
        how_to_fix=(
            f"Enable your terminal (or the Python binary) in "
            f"{_SETTINGS_PANE.format(section='Accessibility')}. "
            "Without it every element resolves to an empty title."
        ),
    )


def check_screen_recording(prompt: bool = False) -> Permission:
    """Whether this process may capture the contents of other apps' windows."""
    if prompt:
        # Triggers the system dialog; returns immediately, grant is async.
        Quartz.CGRequestScreenCaptureAccess()
    granted = bool(Quartz.CGPreflightScreenCaptureAccess())
    return Permission(
        name="Screen Recording",
        granted=granted,
        how_to_fix=(
            f"Enable your terminal (or the Python binary) in "
            f"{_SETTINGS_PANE.format(section='Screen Recording')}, then restart it. "
            "Without it captured frames show only the desktop wallpaper."
        ),
    )


def check_all(prompt: bool = False) -> list[Permission]:
    return [check_accessibility(prompt), check_screen_recording(prompt)]


def input_monitoring_hint() -> str:
    """CGEventTap needs Input Monitoring, which has no preflight API.

    There is no supported way to query this before creating a tap, so the
    recorder detects it the only way available: `CGEventTapCreate` returns None.
    """
    return (
        f"If recording captures no clicks, enable your terminal in "
        f"{_SETTINGS_PANE.format(section='Input Monitoring')} and restart it."
    )
