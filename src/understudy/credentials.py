"""Resolve the Anthropic API key, so nobody has to export it before every command.

Two sources, in precedence order:

  1. the ANTHROPIC_API_KEY environment variable -- wins, so CI and one-off
     overrides keep working
  2. a .env file in the project root, for a key scoped to this project alone

The key is never logged, never written to the manifest, and never passed as a
command-line argument, which would put it in the process list and in shell
history.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "ANTHROPIC_API_KEY"
ENV_FILENAME = ".env"


@dataclass(frozen=True)
class Credential:
    key: str | None
    source: str          # "environment" | ".env file" | "none"

    @property
    def found(self) -> bool:
        return bool(self.key)

    def masked(self) -> str:
        """A form safe to print: enough to recognise the key, not enough to use it."""
        if not self.key:
            return "-"
        return f"{self.key[:8]}...{self.key[-4:]}" if len(self.key) > 16 else "set"


def project_root(start: Path | None = None) -> Path:
    """Nearest ancestor containing a pyproject.toml, else the starting directory."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return current


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=value parser: comments, blank lines and simple quoting.

    Deliberately not a dependency -- this reads one variable from one file, and a
    dotenv library would be more surface area than the job needs.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            values[name] = value
    return values


def resolve(start: Path | None = None) -> Credential:
    """Find the API key, preferring explicit configuration over stored secrets."""
    from_env = os.environ.get(ENV_VAR, "").strip()
    if from_env:
        return Credential(from_env, "environment")

    env_path = project_root(start) / ENV_FILENAME
    if env_path.exists():
        value = parse_env_file(env_path).get(ENV_VAR, "").strip()
        if value:
            return Credential(value, ".env file")

    return Credential(None, "none")


def apply_to_environment(start: Path | None = None) -> Credential:
    """Resolve the key and expose it to the SDK for this process only.

    The Anthropic client reads ANTHROPIC_API_KEY from the environment, so a key
    that came from a .env file is injected here. This mutates only the current
    process; nothing is written back to the shell.
    """
    credential = resolve(start)
    if credential.found and not os.environ.get(ENV_VAR):
        os.environ[ENV_VAR] = credential.key
    return credential


WORKSPACE_ENV_VAR = "ANTHROPIC_WORKSPACE_ID"
KEY_PREFIX = "sk-ant-"
MIN_KEY_LENGTH = 40


def looks_like_key(value: str) -> tuple[bool, str]:
    """Cheap shape check, so `doctor` reports a malformed key as malformed.

    This is deliberately not a network call: it catches the common accident
    (the wrong value pasted into the shell or a .env file) without pretending
    to verify that the key is live. Returns (ok, reason-if-not).
    """
    value = value.strip()
    if not value:
        return False, "nothing entered"
    if not value.startswith(KEY_PREFIX):
        return False, f"an Anthropic key starts with {KEY_PREFIX!r}"
    if len(value) < MIN_KEY_LENGTH:
        return False, f"too short ({len(value)} chars; expected ~100)"
    return True, ""


def resolve_workspace(start: Path | None = None) -> str | None:
    """Workspace id for identity-linked keys, which reject requests without one.

    Same precedence as the key itself: environment first, then .env. Not a
    secret -- it is an identifier -- so it is safe to print in diagnostics.
    """
    from_env = os.environ.get(WORKSPACE_ENV_VAR, "").strip()
    if from_env:
        return from_env
    env_path = project_root(start) / ENV_FILENAME
    if env_path.exists():
        value = parse_env_file(env_path).get(WORKSPACE_ENV_VAR, "").strip()
        if value:
            return value
    return None
