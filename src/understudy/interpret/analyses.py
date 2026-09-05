"""Every analysis of a recording, kept rather than overwritten.

Vocabulary, because the code used to use one word for both halves:

  recording -- one capture session, the operator performing the task once
  analysis  -- one model output over one or more recordings

A recording has many analyses; an analysis covers one or more recordings. That
is why `AnalysisStore` is a class of its own rather than a method on `Recording`:
a synthesis compares several recordings and belongs to none of them, so it needs
somewhere to live that is not a recording directory.

Two analyses of the same trace differ -- the model groups steps differently, and
`--effort` or a different model changes what it sees. Overwriting meant you could
not compare an analysis from before a capture fix with one from after, so each
lands in its own numbered directory and nothing is ever replaced.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

ANALYSES_DIRNAME = "interpretations"
META_FILENAME = "analysis.json"
_LEGACY_META_FILENAME = "run.json"      # written before the rename
_NUMBERED = re.compile(r"^(\d{3})$")


@dataclass(frozen=True)
class Analysis:
    """One saved model output, with the metadata describing how it was made."""

    number: int
    path: Path
    meta: dict

    @property
    def created(self) -> str:
        return str(self.meta.get("created", "?"))

    def load[T: BaseModel](self, model: type[T]) -> T:
        """Rebuild the result. The SOP lives beside the JSON, not inside it."""
        data = json.loads((self.path / "procedure.json").read_text())
        data["sop"] = (self.path / "sop.md").read_text()
        return model.model_validate(data)


class AnalysisStore:
    """A directory holding numbered analyses that are never overwritten."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory

    @property
    def analyses_dir(self) -> Path:
        return self.dir / ANALYSES_DIRNAME

    def list(self) -> list[Analysis]:
        """Saved analyses, oldest first. An unreadable one is skipped, not fatal."""
        base = self.analyses_dir
        if not base.is_dir():
            return []
        found: list[Analysis] = []
        for child in sorted(base.iterdir()):
            match = _NUMBERED.match(child.name)
            if not (match and child.is_dir()):
                continue
            found.append(Analysis(int(match.group(1)), child, _read_meta(child)))
        return found

    def find(self, number: int) -> Analysis | None:
        return next((a for a in self.list() if a.number == number), None)

    def save(self, result: BaseModel, meta: dict) -> Analysis:
        """Write a new numbered analysis and refresh the copies at the root."""
        existing = self.list()
        number = (existing[-1].number + 1) if existing else 1
        path = self.analyses_dir / f"{number:03d}"
        path.mkdir(parents=True, exist_ok=True)

        procedure = result.model_dump(mode="json", exclude={"sop"})
        (path / "sop.md").write_text(result.sop)
        (path / "procedure.json").write_text(json.dumps(procedure, indent=2, ensure_ascii=False))

        full_meta = {
            "number": number,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **meta,
        }
        (path / META_FILENAME).write_text(json.dumps(full_meta, indent=2))

        # The newest analysis is mirrored at the root, so callers that do not
        # care about history keep working unchanged.
        (self.dir / "sop.md").write_text(result.sop)
        (self.dir / "procedure.json").write_text(json.dumps(procedure, indent=2, ensure_ascii=False))
        return Analysis(number, path, full_meta)


def _read_meta(directory: Path) -> dict:
    """Metadata for one analysis, tolerating the pre-rename filename.

    Analyses written before this file was renamed carry `run.json`. They are read
    rather than migrated: rewriting a directory whose whole purpose is to be an
    immutable record would be the wrong trade for saving one `or`.
    """
    for name in (META_FILENAME, _LEGACY_META_FILENAME):
        try:
            return json.loads((directory / name).read_text())
        except (OSError, ValueError):
            continue
    return {}
