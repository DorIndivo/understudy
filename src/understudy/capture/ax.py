"""Resolve a screen coordinate into a named UI element via the macOS Accessibility API.

This is what turns "clicked at (842, 331)" into
`AXButton "Approve" > AXToolbar > AXWindow "Invoices"` -- a target a replay agent
can query and verify, rather than a coordinate that breaks the moment a window moves.

Every function here is best-effort and returns partial data rather than raising:
AX is an enrichment layer, and a recording with no AX data is still a valid
recording (see `trace.models.Element`).
"""

from __future__ import annotations

import logging
from pathlib import Path

import Quartz
from AppKit import NSRunningApplication, NSWorkspace
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCopyElementAtPosition,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementGetPid,
    AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout,
    AXValueGetValue,
    kAXValueCGRectType,
)

from understudy.trace.models import AppContext, Element

log = logging.getLogger(__name__)

# AX calls block on the target app's run loop. An app that is busy (or hung)
# will stall the caller indefinitely without this bound.
MESSAGING_TIMEOUT_S = 0.25

MAX_ANCESTORS = 6

# Chromium exposes a full AX tree only after this attribute is set on the app
# element -- it keeps the tree off by default for performance.
_MANUAL_ACCESSIBILITY = "AXManualAccessibility"

_CHROMIUM_BUNDLE_PREFIXES = (
    "com.google.Chrome",
    "com.microsoft.edgemac",
    "com.brave.Browser",
    "company.thebrowser",      # Arc, Dia
    "com.vivaldi.Vivaldi",
    "org.chromium.Chromium",
)
_BROWSER_BUNDLE_PREFIXES = _CHROMIUM_BUNDLE_PREFIXES + ("com.apple.Safari", "org.mozilla.firefox")

# How many children/siblings to scan when harvesting a label for a bare element.
# Bounded because this runs on the event tap thread.
MAX_LABEL_SCAN = 12
MAX_LABEL_DEPTH = 2

# Roles that carry visible text and can therefore lend their label to a bare
# ancestor or sibling.
_TEXT_ROLES = {"AXStaticText", "AXText", "AXHeading"}

# Above this area (in square points) an element is a pane or panel, not a
# control. Harvesting a label for one produces confident nonsense -- a VS Code
# editor group would otherwise adopt the title of whatever text sits inside it.
MAX_HARVEST_AREA = 400 * 400

_SECURE_ROLES = {"AXSecureTextField"}

_system_wide = None
_manual_accessibility_done: set[int] = set()


def system_wide():
    global _system_wide
    if _system_wide is None:
        _system_wide = AXUIElementCreateSystemWide()
        AXUIElementSetMessagingTimeout(_system_wide, MESSAGING_TIMEOUT_S)
    return _system_wide


def _attr(element, name: str):
    """Read one AX attribute, returning None on any failure.

    AX returns an (error_code, value) tuple; a non-zero code means the attribute
    is unsupported on this element, which is entirely normal.
    """
    try:
        err, value = AXUIElementCopyAttributeValue(element, name, None)
    except Exception:  # pyobjc raises on some malformed elements
        return None
    return value if err == 0 else None


def _str_attr(element, name: str) -> str | None:
    value = _attr(element, name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _frame(element) -> tuple[float, float, float, float] | None:
    raw = _attr(element, "AXFrame")
    if raw is None:
        return None
    ok, rect = AXValueGetValue(raw, kAXValueCGRectType, None)
    if not ok:
        return None
    return (rect.origin.x, rect.origin.y, rect.size.width, rect.size.height)


def is_browser(bundle_id: str | None) -> bool:
    return bool(bundle_id) and bundle_id.startswith(_BROWSER_BUNDLE_PREFIXES)


def _is_electron(running_app) -> bool:
    """Whether an app bundles the Electron framework.

    Electron apps (Slack, Notion, VS Code) are Chromium underneath, so they keep
    their accessibility tree switched off for the same performance reason and may
    respond to the same AXManualAccessibility switch. Detected by looking for the
    framework in the bundle rather than by maintaining a list of app ids.
    """
    try:
        url = running_app.bundleURL()
        if url is None:
            return False
        path = Path(str(url.path()))
    except Exception:
        return False
    return (path / "Contents" / "Frameworks" / "Electron Framework.framework").exists()


def enable_manual_accessibility(pid: int, bundle_id: str | None, running_app=None) -> None:
    """Force a Chromium-based app to build its full AX tree. Idempotent per pid.

    Applies to real browsers and to Electron apps, which share the same engine
    and the same default-off accessibility tree.
    """
    if pid in _manual_accessibility_done:
        return
    chromium = bool(bundle_id) and bundle_id.startswith(_CHROMIUM_BUNDLE_PREFIXES)
    if not chromium and not (running_app is not None and _is_electron(running_app)):
        return
    app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, MESSAGING_TIMEOUT_S)
    try:
        AXUIElementSetAttributeValue(app, _MANUAL_ACCESSIBILITY, True)
    except Exception as exc:
        log.debug("AXManualAccessibility failed for pid %s: %s", pid, exc)
    _manual_accessibility_done.add(pid)


def frontmost_pid() -> int | None:
    """PID of the app owning the frontmost on-screen window.

    Asked of the window server rather than `NSWorkspace.frontmostApplication()`,
    which is fed by notifications and so needs a running CFRunLoop to stay
    current. The recorder has no run loop on this thread, so the NSWorkspace
    answer freezes at whatever was active when the process started -- which
    silently attributed entire multi-app recordings to the launching terminal.
    `CGWindowListCopyWindowInfo` re-reads the window server on every call.
    """
    try:
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
    except Exception as exc:
        log.debug("CGWindowListCopyWindowInfo failed: %s", exc)
        return None
    for window in windows or ():
        # Layer 0 is the normal window level; menus, the dock and overlays sit
        # above it and must not be mistaken for the active app. The list is
        # returned front-to-back, so the first match is the frontmost window.
        if window.get("kCGWindowLayer") == 0:
            pid = window.get("kCGWindowOwnerPID")
            if pid:
                return int(pid)
    return None


def app_for_pid(pid: int | None) -> AppContext:
    """Application identity and focused-window context for a process id."""
    ctx = AppContext()
    if pid is None:
        return ctx
    running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if running is None:
        return ctx
    ctx.name = str(running.localizedName() or "") or None
    ctx.bundle_id = str(running.bundleIdentifier() or "") or None

    enable_manual_accessibility(pid, ctx.bundle_id, running)

    app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, MESSAGING_TIMEOUT_S)
    window = _attr(app, "AXFocusedWindow") or _attr(app, "AXMainWindow")
    if window is not None:
        ctx.window_title = _str_attr(window, "AXTitle")
        if is_browser(ctx.bundle_id):
            # Safari and Chromium both expose the page URL as the window's AXDocument.
            ctx.url = _str_attr(window, "AXDocument")
    return ctx


def frontmost_app() -> AppContext:
    """The active application plus its focused window title and, for browsers, the URL."""
    pid = frontmost_pid()
    if pid is None:
        # Last resort: the cached workspace answer is better than no app at all.
        running = NSWorkspace.sharedWorkspace().frontmostApplication()
        pid = int(running.processIdentifier()) if running is not None else None
    return app_for_pid(pid)


def element_and_app_at(x: float, y: float) -> tuple[Element, AppContext]:
    """Resolve both the element under a point and the app that owns it.

    One `AXUIElementCopyElementAtPosition` serves both answers. Doing it twice
    would double the accessibility work on the event-tap thread, and the tap is
    disabled by macOS if its callback is slow.

    Owning-process is a stronger signal than "what is frontmost" for a click: it
    names the process behind the thing actually clicked, so it cannot drift to a
    window that merely happens to be in front.
    """
    element = Element()
    try:
        err, ref = AXUIElementCopyElementAtPosition(system_wide(), x, y, None)
    except Exception as exc:
        log.debug("AXUIElementCopyElementAtPosition failed at (%s, %s): %s", x, y, exc)
        return element, frontmost_app()
    if err != 0 or ref is None:
        return element, frontmost_app()

    AXUIElementSetMessagingTimeout(ref, MESSAGING_TIMEOUT_S)
    element = _fill(ref, element)

    try:
        err_pid, pid = AXUIElementGetPid(ref, None)
    except Exception as exc:
        log.debug("AXUIElementGetPid failed: %s", exc)
        return element, frontmost_app()
    if err_pid == 0 and pid:
        return element, app_for_pid(int(pid))
    return element, frontmost_app()


def _fill(ref, element: Element, shallow: bool = False) -> Element:
    """Populate an Element from an AX reference."""
    element.resolved = True
    element.role = _str_attr(ref, "AXRole")
    element.subrole = _str_attr(ref, "AXSubrole")
    element.title = _str_attr(ref, "AXTitle")
    element.description = _str_attr(ref, "AXDescription")
    element.help = _str_attr(ref, "AXHelp")
    element.identifier = _str_attr(ref, "AXIdentifier")
    element.frame = None if shallow else _frame(ref)

    enabled = _attr(ref, "AXEnabled")
    element.enabled = bool(enabled) if enabled is not None else None
    focused = _attr(ref, "AXFocused")
    element.focused = bool(focused) if focused is not None else None

    element.secure = element.role in _SECURE_ROLES or element.subrole in _SECURE_ROLES
    # Never read the contents of a password field, not even into memory.
    element.value = None if element.secure else _str_attr(ref, "AXValue")

    element.path = [] if shallow else _ancestor_path(ref)
    if not shallow:
        element.index_in_parent, element.same_role_siblings = _same_role_ordinal(
            ref, _attr(ref, "AXParent")
        )
    if not shallow:
        harvest_label(ref, element)
    return element


def _text_of(ref) -> str | None:
    """Any visible text this element carries directly."""
    return _str_attr(ref, "AXTitle") or _str_attr(ref, "AXValue") or _str_attr(ref, "AXDescription")


def _harvest_from_descendants(ref, depth: int = 0) -> str | None:
    """Find visible text inside an element that has no label of its own.

    An icon-only button or an unlabeled group very often contains the text that
    names it -- a Chromium AXGroup wrapping an AXStaticText is the common case.
    """
    if depth >= MAX_LABEL_DEPTH:
        return None
    children = _attr(ref, "AXChildren")
    if not children:
        return None
    for child in list(children)[:MAX_LABEL_SCAN]:
        role = _str_attr(child, "AXRole")
        if role in _TEXT_ROLES:
            text = _text_of(child)
            if text:
                return text
        deeper = _harvest_from_descendants(child, depth + 1)
        if deeper:
            return deeper
    return None


def _harvest_from_siblings(ref) -> str | None:
    """Find the text label sitting next to an unlabeled control.

    Form fields are frequently unlabeled themselves, with their caption a
    separate static-text sibling ("Invoice ID:" beside an empty AXTextField).
    """
    parent = _attr(ref, "AXParent")
    if parent is None:
        return None
    siblings = _attr(parent, "AXChildren")
    if not siblings:
        return None
    for sibling in list(siblings)[:MAX_LABEL_SCAN]:
        if _str_attr(sibling, "AXRole") in _TEXT_ROLES:
            text = _text_of(sibling)
            if text:
                return text
    return None


def harvest_label(ref, element: Element) -> Element:
    """Fill in a label for an element that exposes none.

    Records where the label came from in `label_source`, because a harvested
    label is a weaker identifier than one the control reports itself -- the
    replay agent and the model should both be able to tell the difference.

    Skipped for panel-sized elements: a label only means something for a
    control, and a large container adopting the text of a distant descendant
    yields a plausible-looking label that is simply wrong.
    """
    if element.title or element.description or element.identifier:
        element.label_source = "element"
        return element
    if element.frame is not None:
        _, _, width, height = element.frame
        if width * height > MAX_HARVEST_AREA:
            return element
    for source, finder in (("descendant", _harvest_from_descendants), ("sibling", _harvest_from_siblings)):
        try:
            text = finder(ref)
        except Exception:
            text = None
        if text:
            element.title = text.strip()[:200]
            element.label_source = source
            return element
    return element


def focused_element(shallow: bool = True) -> Element:
    """The element that currently has keyboard focus.

    Called before each keystroke, for two reasons: it is the only way to know a
    field is secure (so its characters are never buffered), and it names the
    field text is going into -- without which a `type` step says what was typed
    but not where, and the model cannot tell which values are process inputs.

    Defaults to a shallow read (no ancestor walk, no frame) because this runs on
    the event tap thread at typing speed.
    """
    element = Element()
    focused_app = _attr(system_wide(), "AXFocusedApplication")
    if focused_app is None:
        return element
    ref = _attr(focused_app, "AXFocusedUIElement")
    if ref is None:
        return element
    try:
        return _fill(ref, element, shallow=shallow)
    except Exception:
        return element


def element_at(x: float, y: float) -> Element:
    """Resolve the element under a screen point.

    Coordinates are top-left-origin screen *points* -- the same space CGEvent
    reports -- so no flipping is needed here. (Frames, which work in pixels, do
    have to scale; see `capture/frames.py`.)
    """
    element = Element()
    try:
        err, ref = AXUIElementCopyElementAtPosition(system_wide(), x, y, None)
    except Exception as exc:
        log.debug("AXUIElementCopyElementAtPosition failed at (%s, %s): %s", x, y, exc)
        return element
    if err != 0 or ref is None:
        return element

    AXUIElementSetMessagingTimeout(ref, MESSAGING_TIMEOUT_S)
    return _fill(ref, element)


def _same_role_ordinal(ref, parent) -> tuple[int | None, int | None]:
    """Position of `ref` among its parent's children sharing its role.

    Returned 1-based as (index, count). A labelled control needs no ordinal, but
    an `AXGroup` among nine identical `AXGroup`s is only addressable by which one
    it is -- so this is what lets a replay agent pick the right cell in a
    calendar or the right row in a list.
    """
    if parent is None:
        return None, None
    role = _str_attr(ref, "AXRole")
    children = _attr(parent, "AXChildren")
    if not role or not children:
        return None, None
    try:
        peers = [c for c in children if _str_attr(c, "AXRole") == role]
    except Exception as exc:
        log.debug("sibling scan failed: %s", exc)
        return None, None
    for i, peer in enumerate(peers, start=1):
        # AX references compare by identity of the underlying element.
        if peer == ref:
            return i, len(peers)
    return None, len(peers) or None


def _ancestor_path(ref) -> list[str]:
    """Walk up AXParent, innermost first, building a stable-ish selector path.

    Each hop carries its own ordinal when it has same-role siblings, because a
    path of bare roles ("AXGroup > AXGroup > AXWebArea") identifies thousands of
    elements on a real page and so cannot be used to find anything.
    """
    path: list[str] = []
    current = ref
    for _ in range(MAX_ANCESTORS):
        parent = _attr(current, "AXParent")
        if parent is None:
            break
        role = _str_attr(parent, "AXRole") or "AXUnknown"
        label = _str_attr(parent, "AXTitle") or _str_attr(parent, "AXIdentifier")
        hop = f'{role} "{label}"' if label else role
        if not label:
            index, count = _same_role_ordinal(current, parent)
            if index and count and count > 1:
                hop = f"{hop}[{index}/{count}]"
        path.append(hop)
        current = parent
    return path


def focused_element_is_secure() -> bool:
    """Whether keystrokes right now would land in a password field."""
    return focused_element().secure
