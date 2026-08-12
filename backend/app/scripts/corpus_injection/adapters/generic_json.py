"""Generic JSON/JSONL adapter — the slot the synthetic corpus drops into.

A corpus we generate ourselves has no reason to invent a bespoke on-disk format,
so this adapter defines the one the generator should target and parses it. It is
also the fastest way to inject any corpus for which somebody has already written
a converter.

**Accepted layouts** (checked in this order):

1. ``<root>/meetings.jsonl`` — one JSON object per line.
2. ``<root>/**/*.json`` — one object per file (``meeting_id`` defaults to the
   file stem).

**Object schema** — only ``turns`` is required::

    {
      "meeting_id": "synth-000123",
      "title":      "Q3 planning sync",
      "language":   "en",
      "turns": [
        {"speaker": "Alice", "text": "...", "start": 0.0, "end": 4.2},
        ...
      ],
      "metadata": {"anything": "carried into the manifest verbatim"}
    }

**Timings from a generated corpus are always recorded as synthetic**, even when
the generator supplied ``start``/``end`` and even when they are internally
consistent. A number produced by a generator is not a measurement, and the whole
point of the provenance flag is that it tracks *where the number came from*, not
whether it looks plausible. Supplied times are still used (they preserve the
generator's intended pacing and overlap structure) — they are just not laundered
into ``real``.
"""

from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path
from typing import Any

from app.scripts.corpus_injection.adapters.base import CorpusAdapter
from app.scripts.corpus_injection.model import CorpusInfo
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import TimingInfo
from app.scripts.corpus_injection.model import Turn

VERSION_FILE = "VERSION"


class GenericJsonAdapter(CorpusAdapter):
    """Parse a directory of JSON meeting objects (see the module docstring)."""

    def __init__(self, root: Path, key: str = "synthetic", name: str = "") -> None:
        super().__init__(root)
        self.key = key
        self._name = name or key

    @cached_property
    def _records(self) -> dict[str, dict[str, Any]]:
        jsonl = self.root / "meetings.jsonl"
        out: dict[str, dict[str, Any]] = {}
        if jsonl.is_file():
            for line_no, line in enumerate(jsonl.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                record = json.loads(line)
                meeting_id = str(record.get("meeting_id") or f"line-{line_no:06d}")
                out[meeting_id] = record
            return out
        for path in sorted(self.root.rglob("*.json")):
            if path.name == "manifest.json":
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            out[str(record.get("meeting_id") or path.stem)] = record
        return out

    def describe(self) -> CorpusInfo:
        version_path = self.root / VERSION_FILE
        version = version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else ""
        return CorpusInfo(
            key=self.key,
            name=self._name,
            # No VERSION file: fall back to a description of the content itself,
            # so the manifest never records an empty version for a real run.
            version=version or f"unversioned-{len(self._records)}-meetings",
            license_tier="A",
            root=str(self.root),
        )

    def meeting_ids(self) -> list[str]:
        return sorted(self._records)

    def load(self, meeting_id: str) -> MeetingDoc:
        record = self._records[meeting_id]
        turns = [
            Turn(
                turn_index=i,
                speaker=str(turn.get("speaker") or "Unknown").strip(),
                text=str(turn.get("text") or "").strip(),
                start=_as_float(turn.get("start")),
                end=_as_float(turn.get("end")),
            )
            for i, turn in enumerate(record.get("turns", []))
        ]
        extra = dict(record.get("metadata") or {})
        extra.setdefault("license_tier", "A")
        return MeetingDoc(
            corpus=self.key,
            meeting_id=meeting_id,
            title=str(record.get("title") or meeting_id),
            turns=turns,
            language=str(record.get("language") or "en"),
            # Never TIMING_REAL: a generated timestamp is not a measurement.
            timing=TimingInfo(source="synthetic", reference=None),
            extra=extra,
        )


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
