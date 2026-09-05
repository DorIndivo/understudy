"""Command line interface: doctor -> record -> inspect -> interpret."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from understudy import credentials
from understudy.capture.session import RecordingSession
from understudy.permissions import check_all, input_monitoring_hint
from understudy.trace.models import Action, Step
from understudy.trace.recording import Recording, RecordingError

app = typer.Typer(
    add_completion=False,
    help="Record a business process on macOS and interpret it for an agent.",
)
console = Console()

DEFAULT_RECORDINGS = Path("recordings")

# Actions that operate on a UI element, and so can meaningfully carry one.
_TARGETED_ACTIONS = {
    Action.CLICK, Action.DOUBLE_CLICK, Action.RIGHT_CLICK,
    Action.DRAG, Action.SCROLL, Action.TYPE, Action.SECURE_INPUT,
}


def _has_label(step: Step) -> bool:
    t = step.target
    return bool(t.resolved and (t.title or t.description or t.identifier))


def _ax_status(step: Step) -> str:
    """Three distinct outcomes, because they need different remedies."""
    if step.action not in _TARGETED_ACTIONS:
        return "[dim]n/a[/dim]"
    if _has_label(step):
        if step.target.label_source in ("descendant", "sibling"):
            return "[cyan]found[/cyan]"   # label harvested from a neighbour, not self-reported
        return "[green]named[/green]"
    if step.target.resolved:
        return "[yellow]role[/yellow]"   # found the element, but it exposes no label
    return "[red]none[/red]"


@app.command()
def doctor(
    prompt: bool = typer.Option(False, "--prompt", help="Ask macOS to show the grant dialogs."),
) -> None:
    """Check the permissions this tool needs. Run this first."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("Permission")
    table.add_column("Status")
    permissions = check_all(prompt)
    for permission in permissions:
        table.add_row(
            permission.name,
            "[green]granted[/green]" if permission.granted else "[red]missing[/red]",
        )
    console.print(table)

    for permission in permissions:
        if not permission.granted:
            console.print(f"[yellow]{permission.name}:[/yellow] {permission.how_to_fix}")
    console.print(f"[dim]{input_monitoring_hint()}[/dim]")

    key = credentials.api_key()
    if key:
        ok, reason = credentials.looks_like_key(key)
        if ok:
            console.print(f"\nAPI key: [green]found[/green] ({credentials.masked(key)})")
        else:
            # Presence is not validity: a mistyped value reads as "found" and
            # then fails at the first request with an unhelpful 401.
            console.print(f"\nAPI key: [red]malformed[/red] - {reason}")
        workspace = credentials.workspace_id()
        console.print(
            f"Workspace: {workspace}" if workspace
            else "[dim]Workspace: unset (only needed for identity-linked keys)[/dim]"
        )
    else:
        console.print(
            "\nAPI key: [yellow]not set[/yellow] - recording works without one, "
            "but `interpret` needs it. Set ANTHROPIC_API_KEY in your shell, or put "
            "it in a .env file in this directory."
        )

    if all(p.granted for p in permissions):
        console.print("\n[green]Ready to record.[/green]")
    else:
        console.print(
            "\n[red]Fix the above first[/red] - without them recordings look "
            "successful but contain empty elements and blank frames."
        )
        raise typer.Exit(1)


@app.command()
def record(
    seconds: float = typer.Option(15.0, "--seconds", "-s", help="How long to record."),
    out: Path = typer.Option(DEFAULT_RECORDINGS, "--out", "-o", help="Recordings directory."),
    countdown: int = typer.Option(3, "--countdown", help="Seconds before recording starts."),
    name: str | None = typer.Option(None, "--name", help="Recording name (default: timestamp)."),
    goal: str | None = typer.Option(
        None, "--goal", "-g",
        help="One line on what you are about to do. Strongly recommended - it anchors "
             "the whole interpretation.",
    ),
) -> None:
    """Record a few seconds of work."""
    missing = [p for p in check_all() if not p.granted]
    if missing:
        console.print(
            f"[red]Missing permission(s): {', '.join(p.name for p in missing)}.[/red] "
            "Run `understudy doctor` for details."
        )
        raise typer.Exit(1)

    recording_id = name or datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = out / recording_id

    console.print(
        Panel(
            (f"[bold]Goal:[/bold] {goal}\n\n" if goal else "")
            + "Switch to the app you want to record, then do the task normally.\n"
            "[dim]Avoid typing passwords - secure fields are dropped, but keep it clean.[/dim]",
            title="Get ready",
        )
    )
    for remaining in range(countdown, 0, -1):
        console.print(f"[bold]{remaining}...[/bold]")
        time.sleep(1)
    console.print(f"[bold green]Recording {seconds:g}s[/bold green] -> {out_dir}")

    if not goal:
        console.print(
            "[yellow]No --goal given.[/yellow] Interpretation has to infer intent from clicks "
            "alone, which is markedly less reliable."
        )

    session = RecordingSession(
        out_dir, duration_s=seconds, redaction_patterns=Redactor().patterns, goal=goal
    )
    session.run()

    steps = _open(out_dir).normalize(force=True)
    console.print(f"[green]Done.[/green] {len(steps)} steps captured.")
    console.print(f"Inspect with: [bold]understudy inspect {out_dir}[/bold]")


def _open(directory: Path) -> Recording:
    """Open a recording, or fail with the reason rather than a traceback."""
    try:
        return Recording.open(directory)
    except RecordingError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None


@app.command()
def inspect(
    recording: Path = typer.Argument(..., help="A recording directory."),
    renormalize: bool = typer.Option(
        False, "--renormalize", help="Rebuild steps.json from events.jsonl first."
    ),
) -> None:
    """Show a recording as a readable timeline. Do this before spending an API call."""
    rec = _open(recording)
    if renormalize:
        rec.normalize(force=True)
    steps, manifest = rec.steps, rec.manifest

    console.print(
        Panel(
            (f"[bold]Goal:[/bold] {manifest.goal}\n" if manifest.goal else "")
            + f"{manifest.duration_s:.1f}s  |  {len(steps)} steps  |  "
            f"started {manifest.started_at}\n"
            f"permissions: {manifest.permissions}",
            title=rec.id,
        )
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", width=3)
    table.add_column("t", justify="right", width=7)
    table.add_column("dwell", justify="right", width=6)
    table.add_column("app", width=14)
    table.add_column("what happened")
    table.add_column("AX", width=6, justify="center")

    for step in steps:
        table.add_row(
            str(step.index),
            f"{step.t_ms / 1000:.1f}s",
            f"{step.dwell_ms / 1000:.1f}s" if step.dwell_ms > 500 else "",
            (step.app.name or "-")[:14],
            step.summary(),
            _ax_status(step),
        )
    console.print(table)

    # Only steps that act on an element can have one; a context switch or a
    # keyboard shortcut has no target, and counting those as failures would
    # overstate how degraded the trace is.
    targeted = [s for s in steps if s.action in _TARGETED_ACTIONS]
    unlabeled = [s for s in targeted if not _has_label(s)]
    if unlabeled:
        console.print(
            f"[yellow]{len(unlabeled)}/{len(targeted)} targeted steps have no element "
            "label.[/yellow] Interpretation falls back to the cropped frames for those, "
            "and their confidence will be lower."
        )


@app.command()
def interpret(
    recording: Path = typer.Argument(..., help="A recording directory."),
    effort: str = typer.Option("high", "--effort", help="low | medium | high | xhigh | max"),
    max_images: int = typer.Option(40, "--max-images", help="Cap on frames sent to the model."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Count tokens and estimate cost without calling the API."
    ),
    model: str = typer.Option(
        "opus", "--model", "-m", help="opus | sonnet | haiku, or a full model id."
    ),
    list_analyses: bool = typer.Option(
        False, "--list", help="List saved analyses of this recording and exit."
    ),
    show: int | None = typer.Option(
        None, "--show", help="Re-display a saved analysis by number, without calling the API."
    ),
) -> None:
    """Turn a recording into an SOP and an agent-executable procedure.

    Every analysis is kept under `<recording>/interpretations/NNN/`; `--list`
    shows them and `--show N` reprints one for free.
    """
    from understudy.interpret.passes import InterpretPass, report_interpretation
    from understudy.interpret.schema import Interpretation

    rec = _open(recording)

    if list_analyses:
        _show_analysis_table(rec.analyses)
        return

    if show is not None:
        analysis = rec.analyses.find(show)
        if analysis is None:
            console.print(f"[red]No analysis {show:03d}[/red] for this recording. Try --list.")
            raise typer.Exit(1)
        console.print(f"[dim]Analysis {analysis.number:03d} - {analysis.created}[/dim]")
        report_interpretation(console, analysis.load(Interpretation))
        return

    InterpretPass(rec, max_images=max_images).run(
        rec.analyses, model=model, effort=effort, dry_run=dry_run, console=console
    )


def _show_analysis_table(store) -> None:
    saved = store.list()
    if not saved:
        console.print("[yellow]No analyses saved yet.[/yellow]")
        raise typer.Exit(1)
    table = Table(show_header=True, header_style="bold")
    for column in ("#", "when", "model", "effort", "steps", "conf", "tokens", "cached", "cost"):
        table.add_column(column)
    for analysis in saved:
        m = analysis.meta
        table.add_row(
            f"{analysis.number:03d}",
            analysis.created.replace("T", " ").replace("+00:00", ""),
            str(m.get("model", "?")).removeprefix("claude-"),
            str(m.get("effort", "?")),
            f"{m.get('procedure_steps', '?')}",
            str(m.get("confidence", "?")),
            f"{m.get('input_tokens', 0):,}/{m.get('output_tokens', 0):,}",
            f"{m.get('cached_tokens', 0):,}",
            f"${m.get('cost_usd', 0):.3f}",
        )
    console.print(table)
    console.print(f"[dim]Files under {store.analyses_dir}[/dim]")


@app.command()
def synthesize(
    recordings: list[Path] = typer.Argument(..., help="Two or more recordings of one process."),
    out: Path = typer.Option(Path("syntheses"), "--out", help="Where to save the result."),
    name: str | None = typer.Option(None, "--name", help="Name for this synthesis."),
    effort: str = typer.Option("high", "--effort", help="low | medium | high | xhigh | max"),
    model: str = typer.Option("opus", "--model", "-m", help="opus | sonnet | haiku, or a model id."),
    max_images_each: int = typer.Option(12, "--max-images-each", help="Frame cap per recording."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Estimate cost without calling the API."),
) -> None:
    """Recover one procedure, with its branches, by comparing several recordings.

    A single recording shows which path was taken; only several can show what the
    choice turned on. Give it two or more recordings of the same task.
    """
    from understudy.interpret.analyses import AnalysisStore
    from understudy.interpret.passes import SynthesizePass

    opened = [_open(d) for d in recordings]
    out_dir = out / (name or datetime.now().strftime("%Y-%m-%dT%H-%M-%S"))
    # A synthesis belongs to no single recording, so its analyses live in a
    # store of their own rather than inside one of the inputs.
    SynthesizePass(opened, max_images_each=max_images_each).run(
        AnalysisStore(out_dir), model=model, effort=effort, dry_run=dry_run, console=console
    )


if __name__ == "__main__":
    app()
