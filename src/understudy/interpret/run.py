"""The interpretation pass: one Claude call turning a trace into an SOP and a procedure."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import anthropic
import typer
from rich.console import Console

from understudy import credentials
from understudy.interpret.prompt import SYSTEM_PROMPT, build_blocks
from understudy.interpret import versions
from understudy.interpret.schema import Interpretation, Synthesis
from understudy.trace.models import Manifest, Step

MAX_TOKENS = 16000


@dataclass(frozen=True)
class ModelSpec:
    """What a model costs and which request shape it accepts.

    The thinking API differs by generation: the 5-family takes adaptive thinking
    and an `effort` level, while Haiku 4.5 predates both and rejects them. Sending
    the wrong shape is a 400, so the request is built from this table rather than
    assumed.
    """

    id: str
    price_in: float          # dollars per million input tokens
    price_out: float
    adaptive_thinking: bool  # adaptive thinking + output_config.effort

    @property
    def price_cache_read(self) -> float:
        return self.price_in * 0.1       # cache reads bill at a tenth of input

    @property
    def price_cache_write(self) -> float:
        return self.price_in * 1.25      # writing the cache costs a premium once


MODELS: dict[str, ModelSpec] = {
    "opus": ModelSpec("claude-opus-5", 5.0, 25.0, adaptive_thinking=True),
    "sonnet": ModelSpec("claude-sonnet-5", 2.0, 10.0, adaptive_thinking=True),
    "haiku": ModelSpec("claude-haiku-4-5", 1.0, 5.0, adaptive_thinking=False),
}
DEFAULT_MODEL = "opus"


def resolve_model(name: str) -> ModelSpec:
    """Accept either a short name (`sonnet`) or a full model id."""
    if name in MODELS:
        return MODELS[name]
    for spec in MODELS.values():
        if spec.id == name:
            return spec
    known = ", ".join(sorted(MODELS)) + ", or a full model id"
    raise typer.BadParameter(f"unknown model {name!r} - use one of: {known}")



@contextmanager
def _friendly_auth_errors(console: Console):
    """Turn credential and request failures into actionable messages.

    A missing credential surfaces as a TypeError from deep in the SDK's auth
    resolution, which as a raw traceback tells the user nothing about the fix.
    """
    try:
        yield
    except TypeError as exc:
        if "authentication" not in str(exc).lower():
            raise
        console.print(
            "[red]No API credentials found.[/red] Set ANTHROPIC_API_KEY in your "
            "environment, or put it in a .env file."
        )
        raise typer.Exit(1) from None
    except anthropic.AuthenticationError:
        console.print(
            "[red]Credentials were rejected.[/red] Check ANTHROPIC_API_KEY."
        )
        raise typer.Exit(1) from None
    except anthropic.BadRequestError as exc:
        console.print(f"[red]Request rejected:[/red] {exc.message}")
        if "workspace" in str(exc).lower():
            console.print(
                "[yellow]This key is identity-linked.[/yellow] Set "
                "ANTHROPIC_WORKSPACE_ID to the workspace id from "
                "console.anthropic.com > Settings > Workspaces (it looks like "
                "`wrkspc_...`, and is in the URL when you open the workspace)."
            )
        raise typer.Exit(1) from None



def _build_client() -> anthropic.Anthropic:
    """The SDK client, carrying a workspace id when one is configured.

    Identity-linked keys reject every request that does not name the workspace
    it acts in, so the header is required for those; organisation keys ignore it.
    Sent as a default header rather than per-call so `count_tokens` gets it too.
    """
    workspace = credentials.workspace_id()
    if workspace:
        return anthropic.Anthropic(default_headers={"anthropic-workspace-id": workspace})
    return anthropic.Anthropic()


def load_recording(recording_dir: Path) -> tuple[Manifest, list[Step]]:
    manifest = Manifest.model_validate_json((recording_dir / "manifest.json").read_text())
    steps_path = recording_dir / "steps.json"
    if not steps_path.exists():
        raise FileNotFoundError(
            f"{steps_path} not found - run `understudy inspect {recording_dir}` first."
        )
    steps = [Step.model_validate(s) for s in json.loads(steps_path.read_text())]
    return manifest, steps


def interpret_recording(
    recording_dir: Path,
    effort: str = "high",
    max_images: int = 40,
    dry_run: bool = False,
    console: Console | None = None,
    model: str = DEFAULT_MODEL,
) -> Interpretation | None:
    console = console or Console()
    spec = resolve_model(model)
    manifest, steps = load_recording(recording_dir)
    if not steps:
        console.print("[red]This recording has no steps to interpret.[/red]")
        return None

    blocks = build_blocks(recording_dir, manifest, steps, max_images=max_images)
    image_count = sum(1 for b in blocks if b["type"] == "image")
    console.print(f"Sending {len(steps)} steps and {image_count} images to {spec.id}...")

    if not credentials.api_key():
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/red] Export it in your shell "
            "(add it to ~/.zshrc so it survives new terminals)."
        )
        raise typer.Exit(1)
    client = _build_client()
    # The system prompt is identical across recordings, so caching it makes
    # repeat runs materially cheaper.
    system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": blocks}]

    if dry_run:
        with _friendly_auth_errors(console):
            counted = client.messages.count_tokens(
                model=spec.id, system=system, messages=messages
            )
        cost = counted.input_tokens / 1e6 * spec.price_in
        console.print(
            f"[bold]Dry run:[/bold] {counted.input_tokens:,} input tokens on {spec.id} "
            f"(~${cost:.3f} in, plus output at ${spec.price_out}/M)."
        )
        return None

    with _friendly_auth_errors(console):
        # Only the 5-family accepts adaptive thinking and an effort level;
        # sending either to Haiku 4.5 is a 400.
        extra: dict = (
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
            if spec.adaptive_thinking
            else {}
        )
        response = client.messages.parse(
            model=spec.id,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            output_format=Interpretation,
            **extra,
        )

    if response.stop_reason == "refusal":
        console.print("[red]The model declined to process this recording.[/red]")
        return None

    result = response.parsed_output
    usage = response.usage
    version = versions.save(
        recording_dir,
        result,
        {
            "model": spec.id,
            "effort": effort if spec.adaptive_thinking else "n/a",
            "max_images": max_images,
            "steps": len(steps),
            "images": image_count,
            "procedure_steps": len(result.procedure),
            "confidence": result.confidence,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cached_tokens": getattr(usage, "cache_read_input_tokens", 0),
            "cost_usd": round(
                usage.input_tokens / 1e6 * spec.price_in
                + getattr(usage, "cache_read_input_tokens", 0) / 1e6 * spec.price_cache_read
                + getattr(usage, "cache_creation_input_tokens", 0) / 1e6 * spec.price_cache_write
                + usage.output_tokens / 1e6 * spec.price_out,
                4,
            ),
        },
    )
    _report(console, recording_dir, result, response, version)
    return result


def report_result(console: Console, result: Interpretation, where: Path | None = None) -> None:
    """Print an interpretation. Shared by a fresh run and by `--show`."""
    console.print(f"\n[bold]{result.process_name}[/bold]")
    console.print(f"[dim]{result.goal}[/dim]\n")

    for step in result.procedure:
        marker = {"high": "[green]*[/green]", "medium": "[yellow]?[/yellow]", "low": "[red]![/red]"}[
            step.confidence
        ]
        console.print(f" {marker} {step.number}. {step.intent}")

    if result.variables:
        console.print("\n[bold]Variables[/bold] (values that change between runs):")
        for variable in result.variables:
            console.print(f"  {variable.name} = {variable.example!r}  [dim]{variable.description}[/dim]")

    colour = {"high": "green", "medium": "yellow", "low": "red"}[result.confidence]
    console.print(f"\nOverall confidence: [{colour}]{result.confidence}[/{colour}]")
    if result.gaps:
        console.print("[bold]Gaps the recording could not show:[/bold]")
        for gap in result.gaps:
            console.print(f"  - {gap}")
    else:
        console.print("[yellow]No gaps reported - treat that claim with suspicion.[/yellow]")

    if where is not None:
        console.print(f"\n[dim]{where}[/dim]")


def _report(
    console: Console,
    recording_dir: Path,
    result: Interpretation,
    response,
    version: versions.Version | None = None,
) -> None:
    report_result(console, result)
    usage = response.usage
    console.print(
        f"\n[dim]tokens: {usage.input_tokens} in "
        f"({getattr(usage, 'cache_read_input_tokens', 0)} cached), "
        f"{usage.output_tokens} out[/dim]"
    )
    if version is not None:
        console.print(f"[green]Saved run {version.number:03d}[/green] to {version.path}")
        console.print(f"[dim]Latest also mirrored at {recording_dir / 'sop.md'}[/dim]")


def synthesize_recordings(
    recording_dirs: list[Path],
    out_dir: Path,
    effort: str = "high",
    max_images_each: int = 12,
    dry_run: bool = False,
    console: Console | None = None,
    model: str = DEFAULT_MODEL,
) -> Synthesis | None:
    """Recover one procedure, with its branches, from several runs of a process."""
    from understudy.interpret.prompt import SYNTHESIS_SYSTEM_PROMPT, build_synthesis_blocks

    console = console or Console()
    spec = resolve_model(model)
    if len(recording_dirs) < 2:
        console.print("[red]Synthesis needs at least two recordings[/red] to compare.")
        raise typer.Exit(1)

    loaded = []
    for directory in recording_dirs:
        manifest, steps = load_recording(directory)
        if not steps:
            console.print(f"[yellow]Skipping {directory}:[/yellow] no steps.")
            continue
        loaded.append((directory, manifest, steps))
    if len(loaded) < 2:
        console.print("[red]Fewer than two usable recordings.[/red]")
        raise typer.Exit(1)

    blocks = build_synthesis_blocks(loaded, max_images_each=max_images_each)
    images = sum(1 for b in blocks if b["type"] == "image")
    console.print(
        f"Comparing {len(loaded)} runs ({sum(len(s) for _, _, s in loaded)} steps, "
        f"{images} images) with {spec.id}..."
    )

    if not credentials.api_key():
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/red] Export it in your shell "
            "(add it to ~/.zshrc so it survives new terminals)."
        )
        raise typer.Exit(1)

    client = _build_client()
    system = [{
        "type": "text", "text": SYNTHESIS_SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]
    messages = [{"role": "user", "content": blocks}]

    if dry_run:
        with _friendly_auth_errors(console):
            counted = client.messages.count_tokens(
                model=spec.id, system=system, messages=messages
            )
        cost = counted.input_tokens / 1e6 * spec.price_in
        console.print(
            f"[bold]Dry run:[/bold] {counted.input_tokens:,} input tokens on {spec.id} "
            f"(~${cost:.3f} in, plus output at ${spec.price_out}/M)."
        )
        return None

    with _friendly_auth_errors(console):
        extra: dict = (
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
            if spec.adaptive_thinking
            else {}
        )
        response = client.messages.parse(
            model=spec.id,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            output_format=Synthesis,
            **extra,
        )

    if response.stop_reason == "refusal":
        console.print("[red]The model declined to process these recordings.[/red]")
        return None

    result = response.parsed_output
    if not result.procedure:
        console.print(
            "[yellow]The model returned no shared procedure.[/yellow] Saving it anyway "
            "so the output can be inspected, but treat this run as failed."
        )
    usage = response.usage
    version = versions.save(out_dir, result, {
        "model": spec.id,
        "effort": effort if spec.adaptive_thinking else "n/a",
        "kind": "synthesis",
        "recordings": [d.name for d, _, _ in loaded],
        "steps": sum(len(s) for _, _, s in loaded),
        "images": images,
        "procedure_steps": len(result.procedure),
        "branches": len(result.branches),
        "confidence": result.confidence,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_tokens": getattr(usage, "cache_read_input_tokens", 0),
        "cost_usd": round(
            usage.input_tokens / 1e6 * spec.price_in
            + getattr(usage, "cache_read_input_tokens", 0) / 1e6 * spec.price_cache_read
            + getattr(usage, "cache_creation_input_tokens", 0) / 1e6 * spec.price_cache_write
            + usage.output_tokens / 1e6 * spec.price_out, 4,
        ),
    })
    _report_synthesis(console, result, version)
    return result


def _report_synthesis(console: Console, result: Synthesis, version) -> None:
    console.print(f"\n[bold]{result.process_name}[/bold]")
    console.print(f"[dim]{result.goal}[/dim]")
    console.print(f"[dim]from {len(result.recordings)} runs: {', '.join(result.recordings)}[/dim]\n")

    for step in result.procedure:
        marker = {"high": "[green]*[/green]", "medium": "[yellow]?[/yellow]",
                  "low": "[red]![/red]"}[step.confidence]
        console.print(f" {marker} {step.number}. {step.intent}")

    for branch in result.branches:
        colour = {"high": "green", "medium": "yellow", "low": "red"}[branch.confidence]
        console.print(f"\n[bold]Branch after step {branch.after_step}[/bold] "
                      f"on [bold]{branch.deciding_input}[/bold] "
                      f"([{colour}]{branch.confidence}[/{colour}])")
        for observation in branch.observations:
            console.print(f"    {observation.observed_value}  ->  {observation.path_taken}"
                          f"   [dim]({observation.recording_id})[/dim]")
        console.print(f"  [bold]rule:[/bold] {branch.rule}")
        console.print(f"  [bold]bound:[/bold] {branch.bound}")
        for alternative in branch.alternative_explanations:
            console.print(f"  [dim]also consistent with: {alternative}[/dim]")

    colour = {"high": "green", "medium": "yellow", "low": "red"}[result.confidence]
    console.print(f"\nOverall confidence: [{colour}]{result.confidence}[/{colour}]")
    for gap in result.gaps:
        console.print(f"  - {gap}")
    console.print(f"\n[green]Saved run {version.number:03d}[/green] to {version.path}")
