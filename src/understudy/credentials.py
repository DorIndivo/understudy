"""The two API-key questions worth asking before spending a request.

Credentials themselves come from the environment -- the Anthropic SDK reads
`ANTHROPIC_API_KEY` on its own, and `_build_client` reads the workspace id the
same way -- so there is no resolution layer here and nothing to load. What is
left is what the SDK does not do: say whether a key is even shaped like one, and
show it safely.

Both exist because of a real failure: a mistyped value sat in place looking
"found" until the first request failed with an unhelpful 401.
"""

from __future__ import annotations

import os

ENV_VAR = "ANTHROPIC_API_KEY"
WORKSPACE_ENV_VAR = "ANTHROPIC_WORKSPACE_ID"
KEY_PREFIX = "sk-ant-"
MIN_KEY_LENGTH = 40


def api_key() -> str | None:
    return os.environ.get(ENV_VAR, "").strip() or None


def workspace_id() -> str | None:
    """Workspace id for identity-linked keys, which reject requests without one.

    Not a secret -- it is an identifier -- so it is safe to print in diagnostics.
    """
    return os.environ.get(WORKSPACE_ENV_VAR, "").strip() or None


def looks_like_key(value: str) -> tuple[bool, str]:
    """Cheap shape check, so `doctor` reports a malformed key as malformed.

    Deliberately not a network call: it catches the common accident -- the wrong
    value pasted into the shell -- without pretending to verify the key is live.
    Returns (ok, reason-if-not).
    """
    value = value.strip()
    if not value:
        return False, "nothing entered"
    if not value.startswith(KEY_PREFIX):
        return False, f"an Anthropic key starts with {KEY_PREFIX!r}"
    if len(value) < MIN_KEY_LENGTH:
        return False, f"too short ({len(value)} chars; expected ~100)"
    return True, ""


def masked(value: str | None) -> str:
    """A form safe to print: enough to recognise the key, not enough to use it."""
    if not value:
        return "-"
    return f"{value[:8]}...{value[-4:]}" if len(value) > 16 else "set"
