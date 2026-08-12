"""The run manifest — how a published number is traced back to a corpus state.

A retrieval score is only reproducible if you can say exactly what was in the
index when it was measured. The manifest is that record, and it is written by
the tool rather than by hand so it cannot drift from what actually happened.

A run writes three files into its output directory:

``manifest.json``
    Run-level: corpus key + version, seed, tool version, UTC timestamp, the
    resolved target stack, counts, and the synthetic-timing parameters in force.
``files.jsonl``
    One :class:`~.model.InjectionRecord` per meeting — ``file_uuid`` →
    source meeting id, turn/word/speaker counts, duration, and whether the
    timings are real or synthetic.
``turns.jsonl``
    One row per source turn: ``turn_index`` → segment uuid and time span. QMSum
    gold spans address turns by index while the app cites by time and retrieves
    by chunk, so without this table a gold span cannot be mapped onto a
    retrieved chunk at all.

The manifest deliberately records the *target* (host, port, database, index) as
well as the corpus. "Which stack was this measured against?" has already been
the ambiguous question in this repo more than once.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from app.scripts.corpus_injection.model import CorpusInfo
from app.scripts.corpus_injection.model import InjectionRecord
from app.scripts.corpus_injection.timings import SYNTHETIC_PARAMS

MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"
FILES_NAME = "files.jsonl"
TURNS_NAME = "turns.jsonl"


def summarize(records: list[InjectionRecord]) -> dict[str, Any]:
    """Counts a reviewer will ask for, computed once so nobody recomputes them."""
    real = [r for r in records if r.timing_source == "real"]
    return {
        "meetings": len(records),
        "created": sum(1 for r in records if r.action == "created"),
        "updated": sum(1 for r in records if r.action == "updated"),
        "skipped": sum(1 for r in records if r.action == "skipped"),
        "segments": sum(r.segment_count for r in records),
        "turns": sum(r.turn_count for r in records),
        "words": sum(r.word_count for r in records),
        "meetings_with_real_timings": len(real),
        "meetings_with_synthetic_timings": len(records) - len(real),
        "mean_alignment_rate_real": (
            round(sum(r.timing_alignment_rate for r in real) / len(real), 4) if real else 0.0
        ),
    }


def write(
    out_dir: Path,
    corpus: CorpusInfo,
    records: list[InjectionRecord],
    turns: dict[str, list[dict[str, Any]]],
    *,
    seed: str,
    tool_version: str,
    target: dict[str, Any],
    dispatch_mode: str,
    dry_run: bool = False,
) -> Path:
    """Write the three manifest files and return the path to ``manifest.json``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "tool": "app.scripts.corpus_injection",
        "tool_version": tool_version,
        "dry_run": dry_run,
        "dispatch_mode": dispatch_mode,
        "seed": seed,
        "corpus": asdict(corpus),
        "target": target,
        "synthetic_timing_params": dict(SYNTHETIC_PARAMS),
        "counts": summarize(records),
    }
    manifest_path = out_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with (out_dir / FILES_NAME).open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")

    with (out_dir / TURNS_NAME).open("w", encoding="utf-8") as handle:
        for record in records:
            for row in turns.get(record.file_uuid, []):
                handle.write(
                    json.dumps({"file_uuid": record.file_uuid, **row}, sort_keys=True) + "\n"
                )

    return manifest_path


def read_records(out_dir: Path) -> list[InjectionRecord]:
    """Load ``files.jsonl`` back into :class:`~.model.InjectionRecord` objects."""
    path = Path(out_dir) / FILES_NAME
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(InjectionRecord(**json.loads(line)))
    return records
