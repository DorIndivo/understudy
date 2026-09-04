"""App attribution: which application a step is credited to.

Regression cover for a bug that mis-attributed whole multi-app recordings to the
terminal that launched the recorder. `NSWorkspace.frontmostApplication()` is fed
by notifications and needs a running CFRunLoop to stay current; the capture
thread has none, so its answer froze at process start. The window server is
asked instead, on every call.
"""

from __future__ import annotations

from understudy.capture import ax
from understudy.trace.models import AppContext, Element


class TestFrontmostApp:
    def test_uses_the_window_server_pid(self, monkeypatch):
        monkeypatch.setattr(ax, "frontmost_pid", lambda: 4242)
        seen = []
        monkeypatch.setattr(ax, "app_for_pid", lambda pid: seen.append(pid) or AppContext(name="Chrome"))
        assert ax.frontmost_app().name == "Chrome"
        assert seen == [4242]

    def test_falls_back_to_nsworkspace_when_the_window_server_is_silent(self, monkeypatch):
        # Better a stale app than none: the trace stays usable either way.
        monkeypatch.setattr(ax, "frontmost_pid", lambda: None)
        monkeypatch.setattr(ax, "app_for_pid", lambda pid: AppContext(name=f"pid-{pid}"))
        assert ax.frontmost_app().name is not None

    def test_a_missing_pid_yields_an_empty_context_not_an_error(self):
        assert ax.app_for_pid(None) == AppContext()


class TestElementAndAppAt:
    def test_the_app_comes_from_the_element_owner(self, monkeypatch):
        """A click is credited to the process owning what was clicked."""
        monkeypatch.setattr(ax, "system_wide", lambda: object())
        monkeypatch.setattr(ax, "AXUIElementCopyElementAtPosition", lambda *a: (0, object()))
        monkeypatch.setattr(ax, "AXUIElementSetMessagingTimeout", lambda *a: None)
        monkeypatch.setattr(ax, "_fill", lambda ref, el: Element(resolved=True, role="AXButton"))
        monkeypatch.setattr(ax, "AXUIElementGetPid", lambda ref, _: (0, 777))
        monkeypatch.setattr(ax, "app_for_pid", lambda pid: AppContext(name=f"owner-{pid}"))

        element, app = ax.element_and_app_at(10, 20)
        assert element.role == "AXButton"
        assert app.name == "owner-777"

    def test_an_unresolvable_point_still_returns_an_app(self, monkeypatch):
        monkeypatch.setattr(ax, "system_wide", lambda: object())
        monkeypatch.setattr(ax, "AXUIElementCopyElementAtPosition", lambda *a: (-1, None))
        monkeypatch.setattr(ax, "frontmost_app", lambda: AppContext(name="Finder"))

        element, app = ax.element_and_app_at(10, 20)
        assert element.resolved is False   # AX enriches; it never gates the step
        assert app.name == "Finder"

    def test_a_failed_pid_lookup_falls_back_to_frontmost(self, monkeypatch):
        monkeypatch.setattr(ax, "system_wide", lambda: object())
        monkeypatch.setattr(ax, "AXUIElementCopyElementAtPosition", lambda *a: (0, object()))
        monkeypatch.setattr(ax, "AXUIElementSetMessagingTimeout", lambda *a: None)
        monkeypatch.setattr(ax, "_fill", lambda ref, el: Element(resolved=True))
        monkeypatch.setattr(ax, "AXUIElementGetPid", lambda ref, _: (-1, 0))
        monkeypatch.setattr(ax, "frontmost_app", lambda: AppContext(name="Finder"))

        _, app = ax.element_and_app_at(10, 20)
        assert app.name == "Finder"
