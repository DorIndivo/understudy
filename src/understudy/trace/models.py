"""The data contract between capture, normalization and interpretation.

`steps.json` is the interface: capture writes it, interpretation reads it, and a
future replay executor reads it too. Every accessibility-derived field is
Optional by design -- AX is an enrichment, never a gate (see `capture/ax.py`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Action(StrEnum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"
    TYPE = "type"
    KEY = "key"          # a non-text keypress or shortcut, e.g. Tab, Cmd+S
    # Focus entered a password field. macOS turns on secure input mode there,
    # which stops event taps seeing the keystrokes at all -- so the recorder
    # cannot know what, or whether, anything was typed. Emitting a marker keeps
    # the credential step in the process; without it the procedure silently
    # omits authentication and a replaying agent skips the field.
    SECURE_INPUT = "secure_input"
    SCROLL = "scroll"
    CONTEXT_SWITCH = "context_switch"


class Element(BaseModel):
    """A UI element resolved from the accessibility tree at click time.

    All fields optional: an Electron app or a timed-out AX query yields an
    Element with nothing but `resolved=False`, and the pipeline carries on using
    coordinates and the cropped frame instead.
    """

    resolved: bool = False
    role: str | None = None              # AXButton, AXTextField, ...
    subrole: str | None = None
    title: str | None = None             # AXTitle -- the visible label
    value: str | None = None             # AXValue -- full text, even if scrolled out of view
    description: str | None = None       # AXDescription, often set on icon-only buttons
    help: str | None = None              # AXHelp -- the tooltip
    identifier: str | None = None        # AXIdentifier -- often a developer-set test id
    enabled: bool | None = None
    focused: bool | None = None
    frame: tuple[float, float, float, float] | None = None  # x, y, w, h in screen points
    path: list[str] = Field(default_factory=list)  # ancestor chain, innermost first
    # Position among siblings sharing this role. For an element with no unique
    # label -- the common case in web content -- the ordinal is the only thing
    # that distinguishes it from its neighbours, so it is what makes a target
    # re-findable rather than merely describable.
    index_in_parent: int | None = None
    same_role_siblings: int | None = None
    secure: bool = False                 # AXSecureTextField -- never record its contents
    label_source: str | None = None      # element | descendant | sibling -- how the title was obtained
    dom_selector: str | None = None      # reserved for a future CDP source

    def describe(self) -> str:
        """A short human/LLM-readable label, best available field first."""
        if not self.resolved:
            return "unidentified element"
        label = self.title or self.description or self.value or self.identifier
        role = (self.role or "element").removeprefix("AX")
        return f'{role} "{label}"' if label else f"unlabeled {role}"


class AppContext(BaseModel):
    name: str | None = None
    bundle_id: str | None = None
    window_title: str | None = None
    url: str | None = None               # populated for browser windows


class Frames(BaseModel):
    """Paths are relative to the recording directory."""

    pre: str | None = None
    crop: str | None = None
    post: str | None = None


class TypedText(BaseModel):
    """Text entered during a `type` step.

    When the destination was a secure field, `text` is None and only the length
    survives -- redaction happens before this object is ever constructed.
    """

    text: str | None = None
    redacted: bool = False
    length: int = 0


class RawEvent(BaseModel):
    """One low-level event as it came off the tap. Append-only, never rewritten."""

    t_ms: int
    kind: str                            # mouse_down, mouse_up, key_down, scroll, app_change, keyframe
    x: float | None = None
    y: float | None = None
    button: str | None = None
    click_count: int | None = None
    key_code: int | None = None
    chars: str | None = None
    modifiers: list[str] = Field(default_factory=list)
    scroll_dy: float | None = None
    scroll_dx: float | None = None
    element: Element | None = None
    app: AppContext | None = None
    frame_path: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Step(BaseModel):
    """One semantic action. This is what the model reads."""

    index: int
    t_ms: int
    dwell_ms: int = 0                    # idle gap before this step -- long dwell = the user was reading/deciding
    action: Action
    target: Element = Field(default_factory=Element)
    app: AppContext = Field(default_factory=AppContext)
    x: float | None = None
    y: float | None = None
    text: TypedText | None = None
    key: str | None = None               # e.g. "Cmd+S", "Tab"
    scroll_dy: float | None = None
    frames: Frames = Field(default_factory=Frames)

    def summary(self) -> str:
        match self.action:
            case Action.TYPE:
                if self.text and self.text.redacted:
                    body = f"typed {self.text.length} redacted characters into"
                else:
                    body = f'typed "{self.text.text if self.text else ""}" into'
                return f"{body} {self.target.describe()}"
            case Action.KEY:
                return f"pressed {self.key}"
            case Action.SCROLL:
                return f"scrolled {'down' if (self.scroll_dy or 0) < 0 else 'up'} in {self.target.describe()}"
            case Action.SECURE_INPUT:
                return f"entered a secret into {self.target.describe()} (not observable)"
            case Action.CONTEXT_SWITCH:
                # Most context switches inside a browser are navigations, not app
                # changes; naming only the app hides the one detail that matters.
                if self.app.url:
                    return f"navigated to {self.app.url}"
                if self.app.window_title:
                    return f"switched to {self.app.window_title}"
                return f"switched to {self.app.name or 'another app'}"
            case _:
                return f"{self.action.value.replace('_', ' ')} on {self.target.describe()}"


class DisplayInfo(BaseModel):
    index: int
    width: int
    height: int
    scale: float = 1.0                   # backing scale factor; 2.0 on Retina


class Manifest(BaseModel):
    """Session metadata written once at the start of a recording."""

    version: Literal[1] = 1
    recording_id: str
    started_at: str                      # ISO 8601, wall clock
    goal: str | None = None              # what the operator said they were doing
    duration_s: float
    clock_origin_ns: int
    displays: list[DisplayInfo] = Field(default_factory=list)
    permissions: dict[str, bool] = Field(default_factory=dict)
    redaction_patterns: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Recording(BaseModel):
    """A whole recording, as loaded from disk for interpretation."""

    manifest: Manifest
    steps: list[Step]
