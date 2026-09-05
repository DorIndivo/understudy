"""One recording on disk, and everything that knows its layout.

Before this class the directory layout was known in four places -- the CLI's
normalize helper, the CLI's inspect command, the interpretation loader, and the
analysis store -- so adding a file to a recording meant finding all four. The
invariants lived in each caller's head:

  * `steps.json` is derived from `events.jsonl` and can always be rebuilt
  * a recording without a manifest cannot be read
  * analyses live under `interpretations/NNN/`

They live here now. `Recording` is the answer to "what is a recording", which is
the precondition for adding anything to one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from understudy.interpret.analyses import AnalysisStore
from understudy.trace.models import Manifest, RawEvent, Step
from understudy.trace.normalize import normalize, write_steps
from understudy.trace.redact import Redactor

MANIFEST_NAME = "manifest.json"
EVENTS_NAME = "events.jsonl"
STEPS_NAME = "steps.json"
FRAMES_DIRNAME = "frames"


class RecordingError(Exception):
    """A recording directory that cannot be read as one."""


class Recording:
    def __init__(self, directory: Path, manifest: Manifest) -> None:
        self.dir = directory
        self.manifest = manifest
        self.analyses = AnalysisStore(directory)
        self._steps: list[Step] | None = None

    # -- opening and creating ----------------------------------------------------

    @classmethod
    def open(cls, directory: Path) -> "Recording":
        """Load an existing recording, or explain precisely why it cannot be."""
        path = Path(directory)
        manifest_path = path / MANIFEST_NAME
        if not manifest_path.exists():
            # The common cause is a recording interrupted before the manifest was
            # written; the raw events are usually still intact, so say so.
            hint = (
                " The raw events are there, so the recording was interrupted before it "
                "finished." if (path / EVENTS_NAME).exists() else ""
            )
            raise RecordingError(f"{manifest_path} not found - not a recording.{hint}")
        try:
            manifest = Manifest.model_validate_json(manifest_path.read_text())
        except ValueError as exc:
            raise RecordingError(f"{manifest_path} is not a readable manifest: {exc}") from None
        return cls(path, manifest)

    @classmethod
    def create(cls, directory: Path, manifest: Manifest) -> "Recording":
        """Wrap a directory a capture session is about to write into."""
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / MANIFEST_NAME).write_text(manifest.model_dump_json(indent=2))
        return cls(path, manifest)

    # -- identity ----------------------------------------------------------------

    @property
    def id(self) -> str:
        return self.manifest.recording_id

    @property
    def goal(self) -> str | None:
        return self.manifest.goal

    def __repr__(self) -> str:
        return f"Recording({self.id!r}, {len(self.steps)} steps)"

    # -- the trace ---------------------------------------------------------------

    @property
    def steps(self) -> list[Step]:
        """Semantic steps, normalized from the raw events on first access."""
        if self._steps is None:
            steps_path = self.dir / STEPS_NAME
            if steps_path.exists():
                self._steps = [Step.model_validate(s) for s in json.loads(steps_path.read_text())]
            else:
                self._steps = self.normalize()
        return self._steps

    def normalize(self, force: bool = False) -> list[Step]:
        """Rebuild `steps.json` from the events.

        Steps are derived, so this is safe to run at any time -- and necessary
        after a change to `normalize.py`, which is how existing recordings pick up
        new step kinds without being re-recorded.
        """
        if not force and self._steps is not None:
            return self._steps
        steps = normalize(list(self.events()), Redactor(self.manifest.redaction_patterns or None))
        write_steps(steps, self.dir / STEPS_NAME)
        self._steps = steps
        return steps

    def events(self) -> Iterator[RawEvent]:
        """Raw events, streamed.

        A malformed line is skipped rather than aborting the read: one bad line
        in a long capture should cost that event, not the whole recording.
        """
        path = self.dir / EVENTS_NAME
        if not path.exists():
            raise RecordingError(f"{path} not found - nothing was captured.")
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield RawEvent.model_validate_json(line)
                except ValueError:
                    continue

    # -- frames ------------------------------------------------------------------

    @property
    def frames_dir(self) -> Path:
        return self.dir / FRAMES_DIRNAME

    def frame(self, relative: str) -> Path:
        """Absolute path for a frame reference as stored in a step."""
        return self.dir / relative
