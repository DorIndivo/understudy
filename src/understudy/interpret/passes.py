"""Model-calling stages, and the one place that knows how to run one.

Every pass does the same nine things: check for a key, build a client, assemble
blocks, optionally price the request instead of sending it, pick the request
shape the model accepts, call, check for a refusal, save a numbered analysis, and
report. Only two of those differ between passes -- what goes in the prompt, and
how the result is printed.

So `LLMPass` owns the other seven and each subclass supplies `build_blocks` and
`report`. Adding a stage -- an interview over `gaps`, a critic over a procedure --
should mean one new subclass and one new CLI command, and nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import typer
from pydantic import BaseModel
from rich.console import Console

from understudy import credentials
from understudy.interpret.analyses import Analysis, AnalysisStore
from understudy.interpret.client import (
    DEFAULT_MODEL,
    MAX_TOKENS,
    build_client,
    friendly_auth_errors,
    resolve_model,
)
from understudy.interpret.prompt import (
    SYNTHESIS_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_blocks,
    build_synthesis_blocks,
)
from understudy.interpret.schema import Interpretation, Synthesis
from understudy.trace.recording import Recording


class LLMPass[T: BaseModel](ABC):
    """One model call that produces a saved, versioned analysis."""

    name: str
    system_prompt: str
    output_model: type[T]

    # -- what each pass supplies -------------------------------------------------

    @abstractmethod
    def build_blocks(self) -> list[dict]:
        """The user-turn content: step facts and frames."""

    @abstractmethod
    def report(self, console: Console, result: T) -> None:
        """Print the result. The shared footer is added by `run`."""

    def describe_inputs(self) -> str:
        """One line naming what is being sent, shown before the call."""
        return self.name

    def metadata(self, result: T) -> dict:
        """Pass-specific fields recorded alongside the analysis."""
        return {}

    # -- what every pass shares --------------------------------------------------

    def run(
        self,
        store: AnalysisStore,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        dry_run: bool = False,
        console: Console | None = None,
    ) -> T | None:
        console = console or Console()
        spec = resolve_model(model)

        blocks = self.build_blocks()
        images = sum(1 for b in blocks if b["type"] == "image")
        console.print(f"{self.describe_inputs()}, {images} images, with {spec.id}...")

        if not credentials.api_key():
            console.print(
                "[red]ANTHROPIC_API_KEY is not set.[/red] Export it in your shell "
                "(add it to ~/.zshrc so it survives new terminals)."
            )
            raise typer.Exit(1)

        client = build_client()
        # The system prompt is identical across analyses, so caching it makes
        # repeat calls materially cheaper.
        system = [
            {"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}
        ]
        messages = [{"role": "user", "content": blocks}]

        if dry_run:
            with friendly_auth_errors(console):
                counted = client.messages.count_tokens(
                    model=spec.id, system=system, messages=messages
                )
            cost = counted.input_tokens / 1e6 * spec.price_in
            console.print(
                f"[bold]Dry run:[/bold] {counted.input_tokens:,} input tokens on {spec.id} "
                f"(~${cost:.3f} in, plus output at ${spec.price_out}/M)."
            )
            return None

        with friendly_auth_errors(console):
            response = client.messages.parse(
                model=spec.id,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                output_format=self.output_model,
                **spec.request_extras(effort),
            )

        if response.stop_reason == "refusal":
            console.print("[red]The model declined to process this recording.[/red]")
            return None

        result = response.parsed_output
        usage = response.usage
        analysis = store.save(result, {
            "kind": self.name,
            "model": spec.id,
            "effort": effort if spec.adaptive_thinking else "n/a",
            "images": images,
            "confidence": result.confidence,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cached_tokens": getattr(usage, "cache_read_input_tokens", 0),
            "cost_usd": spec.cost(usage),
            **self.metadata(result),
        })

        self.report(console, result)
        self._footer(console, usage, analysis)
        return result

    @staticmethod
    def _footer(console: Console, usage, analysis: Analysis) -> None:
        console.print(
            f"\n[dim]tokens: {usage.input_tokens} in "
            f"({getattr(usage, 'cache_read_input_tokens', 0)} cached), "
            f"{usage.output_tokens} out[/dim]"
        )
        console.print(f"[green]Saved analysis {analysis.number:03d}[/green] to {analysis.path}")


class InterpretPass(LLMPass[Interpretation]):
    """One recording -> an SOP and an agent-executable procedure."""

    name = "interpretation"
    system_prompt = SYSTEM_PROMPT
    output_model = Interpretation

    def __init__(self, recording: Recording, max_images: int = 40) -> None:
        self.recording = recording
        self.max_images = max_images

    def describe_inputs(self) -> str:
        return f"Sending {len(self.recording.steps)} steps"

    def build_blocks(self) -> list[dict]:
        return build_blocks(
            self.recording.dir,
            self.recording.manifest,
            self.recording.steps,
            max_images=self.max_images,
        )

    def metadata(self, result: Interpretation) -> dict:
        return {
            "recordings": [self.recording.id],
            "steps": len(self.recording.steps),
            "procedure_steps": len(result.procedure),
        }

    def report(self, console: Console, result: Interpretation) -> None:
        report_interpretation(console, result)


class SynthesizePass(LLMPass[Synthesis]):
    """Several recordings of one process -> a procedure with its branches."""

    name = "synthesis"
    system_prompt = SYNTHESIS_SYSTEM_PROMPT
    output_model = Synthesis

    def __init__(self, recordings: list[Recording], max_images_each: int = 12) -> None:
        if len(recordings) < 2:
            # A single recording shows which path was taken, never why. Comparing
            # is the whole mechanism, so one input is not a degraded synthesis --
            # it is a different question, and `interpret` is the one that answers it.
            raise typer.BadParameter("synthesis needs at least two recordings to compare")
        self.recordings = recordings
        self.max_images_each = max_images_each

    def describe_inputs(self) -> str:
        steps = sum(len(r.steps) for r in self.recordings)
        return f"Comparing {len(self.recordings)} recordings ({steps} steps)"

    def build_blocks(self) -> list[dict]:
        return build_synthesis_blocks(
            [(r.dir, r.manifest, r.steps) for r in self.recordings],
            max_images_each=self.max_images_each,
        )

    def metadata(self, result: Synthesis) -> dict:
        return {
            "recordings": [r.id for r in self.recordings],
            "steps": sum(len(r.steps) for r in self.recordings),
            "procedure_steps": len(result.procedure),
            "branches": len(result.branches),
        }

    def report(self, console: Console, result: Synthesis) -> None:
        report_synthesis(console, result)


# -- rendering -------------------------------------------------------------------
# Kept as functions rather than methods so `--show N` can reprint a saved analysis
# through the same code path a live one takes.


def report_interpretation(console: Console, result: Interpretation) -> None:
    console.print(f"\n[bold]{result.process_name}[/bold]")
    console.print(f"[dim]{result.goal}[/dim]\n")

    for step in result.procedure:
        console.print(f" {_marker(step.confidence)} {step.number}. {step.intent}")

    if result.variables:
        console.print("\n[bold]Variables[/bold] (values that change between runs):")
        for variable in result.variables:
            console.print(
                f"  {variable.name} = {variable.example!r}  [dim]{variable.description}[/dim]"
            )
    _report_confidence_and_gaps(console, result.confidence, result.gaps)


def report_synthesis(console: Console, result: Synthesis) -> None:
    console.print(f"\n[bold]{result.process_name}[/bold]")
    console.print(f"[dim]{result.goal}[/dim]")
    console.print(
        f"[dim]from {len(result.recordings)} recordings: {', '.join(result.recordings)}[/dim]\n"
    )

    for step in result.procedure:
        console.print(f" {_marker(step.confidence)} {step.number}. {step.intent}")

    for branch in result.branches:
        colour = _COLOUR[branch.confidence]
        console.print(
            f"\n[bold]Branch after step {branch.after_step}[/bold] on "
            f"[bold]{branch.deciding_input}[/bold] ([{colour}]{branch.confidence}[/{colour}])"
        )
        for observation in branch.observations:
            console.print(
                f"    {observation.observed_value}  ->  {observation.path_taken}"
                f"   [dim]({observation.recording_id})[/dim]"
            )
        console.print(f"  [bold]rule:[/bold] {branch.rule}")
        console.print(f"  [bold]bound:[/bold] {branch.bound}")
        for alternative in branch.alternative_explanations:
            console.print(f"  [dim]also consistent with: {alternative}[/dim]")

    _report_confidence_and_gaps(console, result.confidence, result.gaps)


_COLOUR = {"high": "green", "medium": "yellow", "low": "red"}
_MARKERS = {"high": "[green]*[/green]", "medium": "[yellow]?[/yellow]", "low": "[red]![/red]"}


def _marker(confidence: str) -> str:
    return _MARKERS[confidence]


def _report_confidence_and_gaps(console: Console, confidence: str, gaps: list[str]) -> None:
    colour = _COLOUR[confidence]
    console.print(f"\nOverall confidence: [{colour}]{confidence}[/{colour}]")
    if gaps:
        console.print("[bold]Gaps this could not settle:[/bold]")
        for gap in gaps:
            console.print(f"  - {gap}")
    else:
        console.print("[yellow]No gaps reported - treat that claim with suspicion.[/yellow]")
