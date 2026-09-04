"""The structured result of interpreting a recording.

Two deliverables come out of one trace: an SOP a person can review, and a
procedure an agent can execute. They are produced together because they must
agree -- a procedure whose steps don't match the written process is worse than
either one alone.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Variable(BaseModel):
    """A value that changes between runs of this process.

    Distinguishing these from constants is the core generalization the model
    performs: a recording types "INV-2024-113" literally, but the process takes
    an invoice id.
    """

    name: str = Field(description="snake_case identifier, e.g. invoice_id")
    description: str = Field(description="What this value is and where it comes from.")
    example: str = Field(description="The literal value observed in the recording.")
    source: str = Field(
        description="Where the operator got it: prior knowledge, another app, a document, etc."
    )


class Target(BaseModel):
    """How to find the thing an action operates on, most durable identifier first."""

    app: str | None = Field(default=None, description="Application name.")
    window: str | None = Field(default=None, description="Window title, if it identifies the view.")
    url: str | None = Field(default=None, description="Page URL for browser steps.")
    role: str | None = Field(default=None, description="Accessibility role, e.g. AXButton.")
    label: str | None = Field(default=None, description="Visible label or accessibility title.")
    identifier: str | None = Field(default=None, description="AXIdentifier, if the app exposes one.")
    description: str = Field(description="How a person would describe finding this element.")
    ordinal: str | None = Field(
        default=None,
        description=(
            "Which one, when the label does not distinguish it: e.g. '2nd of 9 AXGroup in "
            "the September grid'. Required for unlabeled targets -- without it the element "
            "cannot be re-found."
        ),
    )
    ancestor_path: str | None = Field(
        default=None,
        description="Ancestor chain from the captured step, innermost first, if one was recorded.",
    )
    fallback_point: list[float] | None = Field(
        default=None,
        description="Last-resort screen coordinates. Only meaningful at the recorded window size.",
    )


class ProcedureStep(BaseModel):
    """One executable action, with the intent behind it and how to know it worked."""

    number: int
    action: Literal[
        "click", "double_click", "right_click", "type", "key", "scroll", "drag", "switch_app", "wait"
    ]
    intent: str = Field(description="Why this is done, in business terms, not UI terms.")
    target: Target
    input: str | None = Field(
        default=None,
        description="Text to enter or key to press. Use {variable_name} for values that vary.",
    )
    wait_for: str | None = Field(
        default=None, description="Observable postcondition, taken from the after-frame."
    )
    verify: str | None = Field(
        default=None, description="How an agent confirms this step succeeded before continuing."
    )
    source_steps: list[int] = Field(
        default_factory=list, description="Indices of the recorded steps this came from."
    )
    confidence: Literal["high", "medium", "low"] = "high"


class Interpretation(BaseModel):
    """The complete result: both deliverables plus an honest account of the gaps."""

    process_name: str = Field(description="Short name for this business process.")
    goal: str = Field(description="What the operator was trying to accomplish.")
    applications: list[str] = Field(description="Applications involved.")
    preconditions: list[str] = Field(
        description="What must already be true before the process can start."
    )
    variables: list[Variable] = Field(
        description="Values that would differ on another run of this process."
    )
    procedure: list[ProcedureStep] = Field(description="The executable steps, in order.")
    completion_criteria: str = Field(description="How to know the whole process finished.")
    sop: str = Field(
        description=(
            "The process written as markdown for a human reviewer: goal, preconditions, "
            "numbered steps explaining the why, decision points, completion criteria."
        )
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="Overall confidence that an agent could repeat this from the procedure alone."
    )
    gaps: list[str] = Field(
        description=(
            "What could not be determined from the recording: unobserved branches, ambiguous "
            "intent, steps where the frames and the event data disagree. Be specific and honest; "
            "an empty list claims the recording was completely unambiguous."
        )
    )


class BranchObservation(BaseModel):
    """One run's evidence at a decision point."""

    recording_id: str = Field(description="Which recording this was observed in.")
    observed_value: str = Field(description="The deciding data in that run, e.g. 'EUR 420.00'.")
    path_taken: str = Field(description="What the operator did, e.g. 'Approve for payment'.")


class Branch(BaseModel):
    """A decision point recovered by comparing runs that diverged.

    A single recording can only show which path was taken. Two runs that share a
    prefix and then differ locate the decision, and the data that differs between
    them is the evidence for the condition -- which is why this type exists only
    in synthesis and not in a single interpretation.
    """

    after_step: int = Field(description="Procedure step number this decision follows.")
    deciding_input: str = Field(description="The variable the decision appears to turn on.")
    observations: list[BranchObservation]
    rule: str = Field(description="The rule these runs are consistent with, stated for an agent.")
    bound: str = Field(
        description=(
            "What is still unknown, precisely: name the interval no run fell inside. "
            "Say plainly when the runs are too few to support any rule at all."
        )
    )
    alternative_explanations: list[str] = Field(
        description="Other rules equally consistent with the evidence."
    )
    confidence: Literal["high", "medium", "low"] = "low"


class SynthesisStep(BaseModel):
    """A step of the shared spine.

    Deliberately flatter than `ProcedureStep`: the per-run targets already live in
    each recording's own interpretation, and carrying the full nested target here
    made the combined output schema too large for the API to compile.
    """

    number: int
    action: str = Field(description="click, type, scroll, switch_app, secure_input, ...")
    intent: str = Field(description="Why this is done, in business terms.")
    target: str = Field(description="How to find the element: label, ordinal, app and window.")
    input: str | None = Field(default=None, description="Value entered, as {variable} if it varies.")
    verify: str | None = Field(default=None, description="Observable sign the step worked.")
    varies: bool = Field(
        default=False, description="True if this step differed across runs in a minor way."
    )
    confidence: Literal["high", "medium", "low"] = "high"


class Synthesis(BaseModel):
    """One procedure recovered from several runs of the same process."""

    process_name: str
    goal: str = Field(description="What the process accomplishes, across all runs.")
    recordings: list[str] = Field(description="Recording ids that were compared.")
    preconditions: list[str]
    variables: list[Variable] = Field(
        description=(
            "Values that differ between runs. A value that actually changed across runs "
            "is demonstrably variable, not merely suspected to be."
        )
    )
    procedure: list[SynthesisStep] = Field(description="The shared spine, in order.")
    branches: list[Branch] = Field(
        description="Decision points where the runs diverged. Empty if they never did."
    )
    completion_criteria: str
    sop: str = Field(
        description=(
            "The process as markdown for a human reviewer, covering every observed path, "
            "with conditions and their unknowns stated inline."
        )
    )
    confidence: Literal["high", "medium", "low"]
    gaps: list[str] = Field(
        description="What comparing these runs still could not settle."
    )
