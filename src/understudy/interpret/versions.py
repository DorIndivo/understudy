"""Keep every interpretation of a recording, not just the most recent one.

Two runs over the same trace differ -- the model groups steps differently, and
`--effort` changes how hard it looks. Overwriting meant you could not compare a
run before a capture fix with one after it, or two efforts against each other,
without having thought to copy the files off first. So each run lands in its own
numbered directory and nothing is ever overwritten.

The recording root keeps `sop.md` and `procedure.json` as copies of the newest
run, because that is what anything downstream reasonably expects to find.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from understudy.interpret.schema import Interpretation

RUNS_DIRNAME = "interpretations"
_NUMBERED = re.compile(r"^(\d{3})$")


@dataclass(frozen=True)
class Version:
    number: int
    path: Path
    meta: dict

    @property
    def created(self) -> str:
        return str(self.meta.get("created", "?"))

    def load(self) -> Interpretation:
        data = json.loads((self.path / "procedure.json").read_text())
        data["sop"] = (self.path / "sop.md").read_text()
        return Interpretation.model_validate(data)


def runs_dir(recording_dir: Path) -> Path:
    return recording_dir / RUNS_DIRNAME


def list_versions(recording_dir: Path) -> list[Version]:
    """Saved runs, oldest first. Unreadable directories are skipped, not fatal."""
    base = runs_dir(recording_dir)
    if not base.is_dir():
        return []
    versions: list[Version] = []
    for child in sorted(base.iterdir()):
        match = _NUMBERED.match(child.name)
        if not (match and child.is_dir()):
            continue
        try:
            meta = json.loads((child / "run.json").read_text())
        except (OSError, ValueError):
            meta = {}
        versions.append(Version(int(match.group(1)), child, meta))
    return versions


def find(recording_dir: Path, number: int) -> Version | None:
    return next((v for v in list_versions(recording_dir) if v.number == number), None)


def save(recording_dir: Path, result: BaseModel, meta: dict) -> Version:
    """Write a new numbered run and refresh the root copies."""
    versions = list_versions(recording_dir)
    number = (versions[-1].number + 1) if versions else 1
    path = runs_dir(recording_dir) / f"{number:03d}"
    path.mkdir(parents=True, exist_ok=True)

    procedure = result.model_dump(mode="json", exclude={"sop"})
    (path / "sop.md").write_text(result.sop)
    (path / "procedure.json").write_text(json.dumps(procedure, indent=2, ensure_ascii=False))

    full_meta = {
        "number": number,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **meta,
    }
    (path / "run.json").write_text(json.dumps(full_meta, indent=2))

    # The newest run is also mirrored at the recording root, so callers that do
    # not care about history keep working unchanged.
    (recording_dir / "sop.md").write_text(result.sop)
    (recording_dir / "procedure.json").write_text(
        json.dumps(procedure, indent=2, ensure_ascii=False)
    )
    return Version(number, path, full_meta)
