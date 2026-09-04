"""Assemble a recording into a request: the step trace interleaved with its frames.

The ordering matters. Each image is preceded by the step it belongs to, so the
model never has to infer which action sits between two screenshots. And the two
channels are deliberately both present: the accessibility data says what the
control *is*, the pictures say what the screen *looked like*, and where they
disagree that disagreement is itself a finding.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

from understudy.trace.models import Action, Manifest, Step

REQUEST_IMAGE_WIDTH = 1024   # full frames are downscaled for the request only

# Actions that operate on a UI element. Others (an app switch, a keyboard
# shortcut) have no target, so reporting a missing element for them would invite
# the model to hunt for something that was never there.
_TARGETED_ACTIONS = {
    Action.CLICK, Action.DOUBLE_CLICK, Action.RIGHT_CLICK,
    Action.DRAG, Action.SCROLL, Action.TYPE,
}

SYSTEM_PROMPT = """\
You reverse-engineer business processes from screen recordings.

You receive a recording of a person doing a real task: an ordered list of steps
captured from the operating system, and screenshots taken around each action.

For each click you get up to three images:
  - "Screen before": the full screen just before the action
  - "Cursor close-up": a crop centred on the exact point clicked
  - "Screen after": the full screen ~400ms later, showing what the action caused

Each step also carries accessibility data read from the OS at the moment of the
action: the element's role, label, identifier, and its ancestor chain. This data
is precise when present, but it is often absent or generic -- Electron apps and
some web views expose little or nothing.

Some labels are marked "inferred from text inside it" or "inferred from adjacent
text". Those were not reported by the control itself; they were read off a
neighbouring element because the control exposed no label of its own. They are
usually right and usually worth using, but check them against the close-up image
before relying on one, and lower the step's confidence if they disagree.

How to use the two channels together:
1. When accessibility data names the element, trust it over your reading of the
   pixels. It is read directly from the application.
2. When it is missing or generic ("AXUnknown", "AXGroup", no label), identify the
   target from the cursor close-up instead, and lower that step's confidence.
   An unlabeled element may still carry an ordinal ("2nd of 9 AXGroup") and an
   ancestor chain with ordinals. Put those in the target: for an element with no
   label they are the only thing that distinguishes it from its neighbours, and
   an agent cannot re-find it without them. A screen coordinate is not a target;
   it is a last resort that breaks on any other window size.
3. Derive each step's postcondition from the "Screen after" image: a dialog that
   opened, a field that filled, a row that changed state.
4. If the accessibility data and the images disagree about what was clicked, say
   so in `gaps`. Do not silently pick one.

Your job is to recover the *process*, not to narrate the clicks:

- Infer intent. "Clicked the button labelled Approve" is a click; "approved the
  invoice for payment" is the process step. Write intent in business terms.
- Generalize. A recording contains one run with literal values. Identify which
  values would differ on another run and turn them into named variables; leave
  genuinely fixed values as constants. This is the single most important thing
  you do -- an agent replaying hardcoded values does the wrong work correctly.
- Note where the operator paused. A long dwell before a step usually means they
  were reading or deciding, which often marks a decision point rather than a
  mechanical step.
- Give each procedure step a postcondition and a verification, so an agent can
  tell success from silent failure.
- Choose granularity by state change, not by input event. One procedure step per
  meaningful change in the state of the work. Merge into the step they serve:
  scrolling that only brings a target into view, window and tab switches that
  only reach the place the next action happens, and repeated scrolls through one
  document. Emit a step of its own only when something about the task changed --
  a value was set, a screen was submitted, a decision was taken. Record every
  raw step you merged in `source_steps`, so nothing is lost by merging.
- Do not emit a step for an action that had no effect. A mis-click that opened
  nothing is not part of the process; note it in `gaps` instead.
- A `secure_input` step means focus entered a password field. macOS blocks event
  taps inside those, so no keystrokes were recorded and none ever can be: the
  absence of typing there is a property of the OS, not evidence that nothing was
  typed. Always keep it as a step -- a credential is required at that point --
  and make its value a variable the agent must be supplied, never a literal.
- Never express a scroll as a distance. Scroll amounts are meaningless on another
  window size or display. When a scroll must survive as its own step, express it
  as the condition it was scrolling to reach -- "scroll until the attachment tile
  is visible" -- naming the element the operator went on to act on.

Be honest about limits. A few seconds of recording shows one path through the
process: the happy path, with no error branches and no conditional variants. If
intent is ambiguous, if a step's purpose is unclear, or if the recording starts
mid-process, say so in `gaps` rather than inventing a plausible story. A
confident, wrong procedure is the worst possible output; a correct procedure with
named gaps is genuinely useful.
"""


def _encode(path: Path, max_width: int = REQUEST_IMAGE_WIDTH) -> dict | None:
    """Downscale and base64-encode one frame as an image content block."""
    if not path.exists():
        return None
    try:
        image = Image.open(path)
        image.load()
    except Exception:
        return None
    if image.mode != "RGB":
        image = image.convert("RGB")
    if image.width > max_width:
        ratio = max_width / image.width
        image = image.resize((max_width, int(image.height * ratio)), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=80)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/webp",
            "data": base64.standard_b64encode(buffer.getvalue()).decode("ascii"),
        },
    }


MAX_LABEL_CHARS = 120


def _clip(value: str) -> str:
    """Shorten a runaway accessibility label to its identifying head.

    Some controls report their entire text content as a title -- a whole inbox
    row, a full article paragraph. The first line identifies the element; the
    rest pushes the fields that follow out of easy reach.
    """
    value = " ".join(value.split())
    if len(value) <= MAX_LABEL_CHARS:
        return value
    return value[:MAX_LABEL_CHARS].rstrip() + f"... [+{len(value) - MAX_LABEL_CHARS} chars]"


def _step_facts(step: Step) -> str:
    """The step as compact labelled facts rather than raw JSON.

    Prose-shaped facts read better than a JSON dump and cost fewer tokens, and
    absent accessibility fields are simply omitted rather than shown as nulls.
    """
    parts = [f"time {step.t_ms / 1000:.1f}s", f"action {step.action.value}"]
    if step.dwell_ms > 800:
        parts.append(f"paused {step.dwell_ms / 1000:.1f}s before this")
    if step.app.name:
        parts.append(f"app {step.app.name}")
    if step.app.window_title:
        parts.append(f"window {step.app.window_title!r}")
    if step.app.url:
        parts.append(f"url {step.app.url}")

    target = step.target
    if target.resolved:
        described = [f"role {target.role}"] if target.role else []
        # A label read off a neighbouring element is weaker evidence than one the
        # control reports itself, and the model should weigh it accordingly.
        label_key = {
            "descendant": "label (inferred from text inside it)",
            "sibling": "label (inferred from adjacent text)",
        }.get(target.label_source or "", "label")
        for label, value in (
            (label_key, target.title),
            ("description", target.description),
            ("value", target.value),
            ("identifier", target.identifier),
            ("tooltip", target.help),
        ):
            if value:
                described.append(f"{label} {_clip(value)!r}")
        if target.enabled is False:
            described.append("DISABLED")
        # Only worth saying when there is something to disambiguate against.
        if target.index_in_parent and (target.same_role_siblings or 0) > 1:
            described.append(
                f"ordinal {target.index_in_parent} of {target.same_role_siblings} "
                f"{target.role or 'sibling'}"
            )
        if target.path:
            described.append("inside " + " > ".join(target.path[:4]))
        parts.append("element: " + ", ".join(described) if described
                     else "element: present but unlabeled")
    elif step.action in _TARGETED_ACTIONS:
        hint = (
            " - identify it from the close-up image"
            if step.frames.crop
            else " and no close-up image is available"
        )
        parts.append("element: NOT RESOLVED" + hint)

    if step.text:
        if step.text.redacted:
            parts.append(f"typed {step.text.length} characters (REDACTED - do not guess them)")
        else:
            parts.append(f"typed {step.text.text!r}")
    if step.key:
        parts.append(f"key {step.key}")
    if step.scroll_dy:
        parts.append(f"scrolled {'down' if step.scroll_dy < 0 else 'up'}")
    if step.x is not None:
        parts.append(f"at ({step.x:.0f}, {step.y:.0f})")
    return " | ".join(parts)


def build_blocks(
    recording_dir: Path, manifest: Manifest, steps: list[Step], max_images: int = 40
) -> list[dict]:
    """Interleave step facts with their frames, newest-value-first ordering preserved.

    When the image budget is tight, "before" frames are dropped first: consecutive
    after/before frames are near-duplicates, while the close-up (which identifies
    the target) and the after frame (which shows the effect) each carry unique
    information.
    """
    apps = sorted({s.app.name for s in steps if s.app.name})
    goal_line = (
        f"\nThe operator described their goal as: {manifest.goal!r}\n"
        "Treat this as their stated intent, not as a verified description: use it to "
        "interpret ambiguous steps, but if the recording plainly shows something else "
        "happening, trust the recording and note the discrepancy in `gaps`.\n"
        if manifest.goal
        else "\nThe operator did not state a goal, so intent must be inferred entirely "
        "from the actions. Be correspondingly more cautious about claiming intent.\n"
    )
    header = (
        f"# Recording {manifest.recording_id}\n"
        f"Duration {manifest.duration_s:.1f}s, {len(steps)} steps, "
        f"applications: {', '.join(apps) or 'unknown'}.\n"
        f"Displays: {', '.join(f'{d.width}x{d.height}' for d in manifest.displays)}.\n"
        + goal_line
        + "\nThe steps follow in order, each with its screenshots."
    )
    blocks: list[dict] = [{"type": "text", "text": header}]

    budget = _plan_images(steps, max_images)
    for step in steps:
        blocks.append({"type": "text", "text": f"\n## Step {step.index}\n{_step_facts(step)}"})
        for kind, caption in (
            ("pre", "Screen before:"),
            ("crop", "Cursor close-up (what was clicked):"),
            ("post", "Screen after:"),
        ):
            if (step.index, kind) not in budget:
                continue
            path = getattr(step.frames, kind)
            if not path:
                continue
            block = _encode(recording_dir / path)
            if block is None:
                continue
            blocks.append({"type": "text", "text": caption})
            blocks.append(block)

    blocks.append(
        {
            "type": "text",
            "text": (
                "\nRecover the business process from the above. Remember: infer intent, "
                "turn run-specific values into named variables, give every step a way to "
                "verify it worked, and record honestly in `gaps` whatever the recording "
                "could not show you."
            ),
        }
    )
    return blocks


def _plan_images(steps: list[Step], max_images: int) -> set[tuple[int, str]]:
    """Choose which frames fit the budget, in order of information value."""
    selected: set[tuple[int, str]] = set()
    for kind in ("crop", "post", "pre"):     # most to least informative
        for step in steps:
            if len(selected) >= max_images:
                return selected
            if getattr(step.frames, kind):
                selected.add((step.index, kind))
    return selected


SYNTHESIS_SYSTEM_PROMPT = """\
You recover a business process by comparing several recordings of the *same* task.

Each recording is one run: the same steps captured from the operating system, with
screenshots. You have seen these individually before; the point of comparing them
is to recover what no single run can show.

A single recording shows which path an operator took. It cannot show *why*, because
nothing in one run distinguishes "this is the rule" from "this is what happened
that time". Several runs can: where they agree is the procedure, where they diverge
is a decision, and the data that differs at the divergence is the evidence for the
condition. Recovering those conditions is the main thing you are for.

How to compare:
1. Align the runs step by step. Steps that appear in every run, doing the same
   work, are the shared spine -- the `procedure`. Small differences in scrolling,
   window focus or click counts are noise, not divergence.
2. Find the points where runs genuinely differ in *what was accomplished*, not in
   how it was reached. Each is a `branch`.
3. At each branch, look at what was on screen before the decision in each run and
   identify what differed. State the rule those runs are consistent with.
4. State the bound precisely. Two runs at €420 and €2,480.75 that went different
   ways place a threshold somewhere between them -- they do not establish where.
   Say so: name the interval, and say no run was observed inside it.
5. List the alternative explanations you cannot rule out. If runs differ in
   supplier *and* amount, the supplier is as good a candidate as the amount, and
   claiming the amount is the rule would be a guess presented as a finding.

A value that actually changed between runs is demonstrably a variable. That is much
stronger evidence than the single-run case, where a variable is only suspected.
Treat a value that stayed identical across every run as a constant unless there is
a reason to think otherwise, and say which it is.

Be honest about how little two or three runs can establish. A rule supported by two
observations is a hypothesis consistent with the evidence, not a discovered fact --
mark those `low` confidence, and say in `gaps` what further run would settle it
(a specific value to test, not "more data"). If the runs diverged and you cannot
tell what drove it, say that plainly rather than choosing the most plausible story.
A confident wrong rule is worse than an acknowledged unknown: an agent applying it
does the wrong work on every future run, not just once.

What to produce, every time:
- `procedure`: the shared spine as numbered steps. This is never empty -- runs of
  the same process always share something, and if they truly shared nothing, say
  so in `gaps` and give the steps of the run that best represents the process.
- `branches`: one entry per divergence. Empty only if the runs never diverged.
- `sop`: the full markdown write-up for a human, covering every observed path with
  its condition stated inline. Never leave this empty.
- `gaps`: what comparison still could not settle, and the specific further run that
  would settle it.
"""


def build_synthesis_blocks(
    recordings: list[tuple[Path, Manifest, list[Step]]], max_images_each: int = 12
) -> list[dict]:
    """Serialize several recordings for comparison, each clearly delimited.

    The per-recording image budget is deliberately small: across N runs the frames
    dominate the payload, and the comparison turns mostly on the step facts and on
    what differed in the data, not on pixel detail.
    """
    blocks: list[dict] = [{
        "type": "text",
        "text": (
            f"# {len(recordings)} runs of the same process\n\n"
            "Each section below is one complete run, in the same format you would "
            "receive individually. Compare them: recover the shared procedure, the "
            "points where they diverged, and what the divergence turned on."
        ),
    }]
    for index, (directory, manifest, steps) in enumerate(recordings, start=1):
        blocks.append({
            "type": "text",
            "text": f"\n\n===== RUN {index} of {len(recordings)}: {manifest.recording_id} =====",
        })
        blocks.extend(build_blocks(directory, manifest, steps, max_images=max_images_each))
    return blocks
