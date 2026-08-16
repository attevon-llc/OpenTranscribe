"""Corpus assembly and the on-disk layout.

Determinism contract: with the same ``seed`` and config, every file listed in
``SHA256SUMS`` is byte-identical. ``README.md`` and ``MANIFEST.tsv`` are deliberately
**outside** that set because they carry a wall-clock generation stamp; everything a
measurement could depend on is inside it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .org import build_org
from .queries import PlannedQuery
from .queries import plan_queries
from .render import render_session

GENERATOR_VERSION = "1.0.0"

#: Files whose bytes the determinism claim covers.
DETERMINISTIC_GLOBS = (
    "config.json",
    "facts.jsonl",
    "qrels-files.tsv",
    "queries.jsonl",
    "stats.json",
    "meetings/*.jsonl",
)


def default_config(**overrides: Any) -> dict:
    """Return the default corpus config, with ``overrides`` applied.

    Query counts default to a fixed ratio of the meeting count so every rung of the
    scale ladder is generated the same way.
    """
    meetings = int(overrides.pop("meetings", 2000))
    config: dict[str, Any] = {
        "corpus_id": "otsynth-core-v1",
        "generator_version": GENERATOR_VERSION,
        "seed": 20260812,
        "meetings": meetings,
        "meetings_per_team": 40,
        "near_duplicate_rate": 0.15,
        "shard_size": 250,
        "queries": {
            "lookup": max(24, meetings // 4),
            "multi_file": max(12, meetings // 8),
            "aggregation": max(10, meetings // 12),
            "summarize": max(4, meetings // 25),
            "verbatim_control_fraction": 0.2,
        },
    }
    queries_override = overrides.pop("queries", None)
    config.update(overrides)
    if queries_override:
        config["queries"].update(queries_override)
    config["generator_version"] = GENERATOR_VERSION
    return config


def _dumps(obj: Any) -> str:
    """Canonical JSON: sorted keys, no incidental whitespace, UTF-8 text."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")


def build_corpus(config: dict, out_dir: Path) -> dict:
    """Generate the corpus and write it to ``out_dir``.

    Args:
        config: A config from :func:`default_config`.
        out_dir: Target directory; created if absent.

    Returns:
        The ``stats.json`` payload.
    """
    out_dir = Path(out_dir)
    (out_dir / "meetings").mkdir(parents=True, exist_ok=True)
    org = build_org(config)
    queries = plan_queries(org, config)
    by_id = {q.query_id: q for q in queries}

    shard: list[str] = []
    shard_index = 0
    fact_rows: list[str] = []
    stats_acc = _StatsAccumulator()
    written: list[Path] = []
    for session in org.sessions:
        meeting, placements = render_session(session, org, config)
        stats_acc.add(meeting, session)
        for placement in placements:
            query = by_id[placement["query_id"]]
            query.gold_turns.setdefault(placement["file_uuid"], []).append(placement["span"])
            fact_rows.append(
                _dumps(
                    {
                        "query_id": placement["query_id"],
                        "fact_id": placement.get("fact_id"),
                        "kind": placement["kind"],
                        "rule": query.rule,
                        "file_uuid": placement["file_uuid"],
                        "meeting_key": meeting["meeting_key"],
                        "anchor": placement["anchor"],
                        "turn_span": placement["span"],
                        "answer_turn": placement["answer_turn"],
                        "template": placement["template"],
                    }
                )
            )
        shard.append(_dumps(meeting))
        if len(shard) >= config["shard_size"]:
            written.append(_flush_shard(out_dir, shard_index, shard))
            shard, shard_index = [], shard_index + 1
    if shard:
        written.append(_flush_shard(out_dir, shard_index, shard))

    for query in queries:
        for spans in query.gold_turns.values():
            spans.sort()
    _write_lines(out_dir / "queries.jsonl", [_dumps(_query_row(q)) for q in queries])
    _write_lines(out_dir / "facts.jsonl", sorted(fact_rows))
    _write_lines(out_dir / "qrels-files.tsv", _qrels_rows(queries))
    (out_dir / "config.json").write_text(_dumps(config) + "\n", encoding="utf-8", newline="\n")
    stats = stats_acc.finalise(config, queries, org)
    (out_dir / "stats.json").write_text(
        json.dumps(stats, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    write_checksums(out_dir)
    return stats


def _flush_shard(out_dir: Path, index: int, rows: list[str]) -> Path:
    path = out_dir / "meetings" / f"part-{index:04d}.jsonl"
    _write_lines(path, rows)
    return path


def _query_row(query: PlannedQuery) -> dict:
    row = asdict(query)
    row["gold_turns"] = {k: v for k, v in sorted(query.gold_turns.items())}
    return row


def _qrels_rows(queries: list[PlannedQuery]) -> list[str]:
    """TREC qrels at file granularity: ``qid 0 docid rel``.

    Binary at this level on purpose. Graded relevance belongs at chunk level and is
    derived from ``gold_turns`` overlap by the harness, exactly as the QMSum adapter
    does it — one convention for both corpora, not two.
    """
    rows = []
    for query in queries:
        for file_uuid in sorted(query.gold_files):
            rows.append(f"{query.query_id}\t0\t{file_uuid}\t1")
    return rows


def iter_deterministic_files(out_dir: Path) -> list[Path]:
    """Return every file the determinism claim covers, in stable order."""
    out: list[Path] = []
    for pattern in DETERMINISTIC_GLOBS:
        out.extend(sorted(Path(out_dir).glob(pattern)))
    return out


def write_checksums(out_dir: Path) -> Path:
    """Write ``SHA256SUMS`` over the deterministic file set and return its path."""
    out_dir = Path(out_dir)
    lines = []
    for path in iter_deterministic_files(out_dir):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(out_dir).as_posix()}")
    target = out_dir / "SHA256SUMS"
    _write_lines(target, lines)
    return target


class _StatsAccumulator:
    """Collects corpus statistics in one streaming pass."""

    def __init__(self) -> None:
        self.words: list[int] = []
        self.turns: list[int] = []
        self.speakers: list[int] = []
        self.registers: dict[str, int] = {}
        self.clustered = 0

    def add(self, meeting: dict, session) -> None:
        """Fold one rendered meeting into the running statistics."""
        self.words.append(meeting["word_count"])
        self.turns.append(meeting["turn_count"])
        self.speakers.append(len(meeting["speakers"]))
        self.registers[meeting["register"]] = self.registers.get(meeting["register"], 0) + 1
        if session.cluster_id:
            self.clustered += 1

    def finalise(self, config: dict, queries: list[PlannedQuery], org) -> dict:
        """Return the ``stats.json`` payload."""

        def summary(values: list[int]) -> dict:
            ordered = sorted(values)
            n = len(ordered)
            return {
                "n": n,
                "min": ordered[0],
                "p10": ordered[n // 10],
                "median": ordered[n // 2],
                "mean": round(sum(ordered) / n, 2),
                "p90": ordered[(9 * n) // 10],
                "max": ordered[-1],
            }

        classes: dict[str, int] = {}
        surfaces: dict[str, int] = {}
        rules: dict[str, int] = {}
        gold_sizes: dict[str, list[int]] = {}
        for query in queries:
            classes[query.query_class] = classes.get(query.query_class, 0) + 1
            surfaces[query.surface] = surfaces.get(query.surface, 0) + 1
            rules[query.rule] = rules.get(query.rule, 0) + 1
            gold_sizes.setdefault(query.query_class, []).append(len(query.gold_files))
        return {
            "corpus_id": config["corpus_id"],
            "generator_version": config["generator_version"],
            "seed": config["seed"],
            "meetings": len(self.words),
            "teams": len(org.teams),
            "series": len(org.series),
            "total_words": sum(self.words),
            "total_turns": sum(self.turns),
            "words_per_meeting": summary(self.words),
            "turns_per_meeting": summary(self.turns),
            "speakers_per_meeting": summary(self.speakers),
            "registers": dict(sorted(self.registers.items())),
            # The config value is the per-session probability of JOINING the previous
            # session's cluster; the realised figure counts every meeting in a cluster of
            # size >= 2, cluster heads included. The second is therefore always the larger,
            # and calling them "requested"/"realised" invited the reading that the dial
            # missed its target by 11 points when it did no such thing.
            "near_duplicate_join_probability": config["near_duplicate_rate"],
            "near_duplicate_fraction_in_clusters": round(
                self.clustered / max(1, len(self.words)), 4
            ),
            "queries_total": len(queries),
            "queries_by_class": dict(sorted(classes.items())),
            "queries_by_surface": dict(sorted(surfaces.items())),
            "queries_by_rule": dict(sorted(rules.items())),
            "gold_files_per_query": {
                k: round(sum(v) / len(v), 3) for k, v in sorted(gold_sizes.items())
            },
        }
