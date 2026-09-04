"""Scrub sensitive text before it is written to disk or sent to the API.

Two layers, because they catch different things:

  structural  a field the accessibility API reports as AXSecureTextField -- its
              keystrokes are dropped at capture time and never buffered at all
  pattern     a regex denylist applied to text that *did* get buffered, for
              secrets typed into ordinary fields (an API key pasted into a search
              box, a card number in a plain text input)

The defaults below are deliberately broad: a false positive costs the model a
little context, a false negative writes a live credential to disk.
"""

from __future__ import annotations

import re

from understudy.trace.models import TypedText

DEFAULT_PATTERNS: tuple[str, ...] = (
    r"sk-[A-Za-z0-9_\-]{16,}",                     # OpenAI/Anthropic-style API keys
    r"gh[pousr]_[A-Za-z0-9]{16,}",                 # GitHub tokens
    r"AKIA[0-9A-Z]{16}",                           # AWS access key ids
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",  # JWTs
    r"\b(?:\d[ -]*?){13,16}\b",                    # payment card numbers
    r"\b\d{3}-\d{2}-\d{4}\b",                      # US SSN
)

PLACEHOLDER = "[REDACTED]"


class Redactor:
    def __init__(self, patterns: list[str] | None = None):
        self.patterns = list(patterns) if patterns is not None else list(DEFAULT_PATTERNS)
        self._compiled = [re.compile(p) for p in self.patterns]

    def scrub(self, text: str) -> tuple[str, bool]:
        """Replace any matched secret. Returns the text and whether anything matched."""
        redacted = False
        for pattern in self._compiled:
            text, count = pattern.subn(PLACEHOLDER, text)
            if count:
                redacted = True
        return text, redacted

    def typed(self, text: str, secure: bool, count: int | None = None) -> TypedText:
        """Build a TypedText, dropping content entirely for secure fields.

        `count` is the number of keystrokes, needed for secure fields where the
        characters were never captured and `len(text)` would report zero.
        """
        if secure:
            return TypedText(text=None, redacted=True, length=count if count is not None else len(text))
        scrubbed, redacted = self.scrub(text)
        return TypedText(text=scrubbed, redacted=redacted, length=len(text))
