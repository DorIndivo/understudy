"""Which model to call, what it costs, and how to build a client for it."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import anthropic
import typer
from rich.console import Console

from understudy import credentials

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

    def request_extras(self, effort: str) -> dict:
        """Thinking and effort parameters, only for models that accept them."""
        if not self.adaptive_thinking:
            return {}
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}

    def cost(self, usage) -> float:
        """Dollars for one response, counting cache reads and writes at their own rates."""
        return round(
            usage.input_tokens / 1e6 * self.price_in
            + getattr(usage, "cache_read_input_tokens", 0) / 1e6 * self.price_cache_read
            + getattr(usage, "cache_creation_input_tokens", 0) / 1e6 * self.price_cache_write
            + usage.output_tokens / 1e6 * self.price_out,
            4,
        )


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


def build_client() -> anthropic.Anthropic:
    """The SDK client, carrying a workspace id when one is configured.

    Identity-linked keys reject every request that does not name the workspace
    it acts in, so the header is required for those; organisation keys ignore it.
    Sent as a default header rather than per-call so `count_tokens` gets it too.
    """
    workspace = credentials.workspace_id()
    if workspace:
        return anthropic.Anthropic(default_headers={"anthropic-workspace-id": workspace})
    return anthropic.Anthropic()


@contextmanager
def friendly_auth_errors(console: Console):
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
            "[red]No API credentials found.[/red] Set ANTHROPIC_API_KEY in your environment."
        )
        raise typer.Exit(1) from None
    except anthropic.AuthenticationError:
        console.print("[red]Credentials were rejected.[/red] Check ANTHROPIC_API_KEY.")
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
