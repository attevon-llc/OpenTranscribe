"""Loading a finished run directory for the post-run commands."""

from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from tests.eval.harness.summary_scoring import CellScore, GoldRef, extract_merged

from summary_bench.config import ExitCode, HarnessError
from summary_bench.inputs import INPUT_SETS, CorpusPaths, InputSpec, RenderedInput
from summary_bench.planner import read_lock, verify_lock
from summary_bench.runner import RECORDS_NAME, load_records
from summary_bench.scoring import gold_refs, score_records


@dataclass
class Run:
    """A finished run: its records, the lock it ran under, and everything derived from them."""

    dir: Path
    meta: dict[str, Any]
    lock: dict[str, Any]
    records: dict[str, dict[str, Any]]
    specs: dict[str, InputSpec]
    paths: CorpusPaths
    _scored: list[tuple[dict[str, Any], CellScore]] | None = field(default=None, repr=False)

    @cached_property
    def gold(self) -> dict[str, list[GoldRef]]:
        return {
            input_id: gold_refs(spec, self.paths, self.lock['inputs'][input_id])
            for input_id, spec in self.specs.items()
        }

    @property
    def scored(self) -> list[tuple[dict[str, Any], CellScore]]:
        if self._scored is None:
            self._scored = score_records(self.records, self.lock, self.specs, self.paths)
        return self._scored

    @cached_property
    def rendered(self) -> dict[str, RenderedInput]:
        """Re-rendered inputs, verified against the lock (they hold corpus text: memory only)."""
        return verify_lock(self.lock, tuple(self.specs.values()), self.paths)


def load_run(args: Namespace) -> Run:
    run_dir = Path(args.run_dir).expanduser()
    meta_path = run_dir / 'meta.json'
    if not meta_path.exists():
        raise HarnessError(
            f'{run_dir} is not a run directory (no meta.json)', ExitCode.PRECONDITION
        )
    meta = json.loads(meta_path.read_text())
    records = load_records(run_dir / RECORDS_NAME)
    if not records:
        raise HarnessError(f'{run_dir} has no records yet', ExitCode.PRECONDITION)
    lock = read_lock(Path(args.lock).expanduser() if getattr(args, 'lock', None) else run_dir)
    specs = {s.input_id: s for s in INPUT_SETS[meta['input_set']]}
    return Run(run_dir, meta, lock, records, specs, CorpusPaths.under(Path(args.data_dir)))


def merged_text_of(record: dict[str, Any]) -> str:
    """The merged summary's text (all fields), for the judge. Corpus-derived: never persisted."""
    return extract_merged(record.get('merged')).text
