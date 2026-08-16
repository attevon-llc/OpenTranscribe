"""Brute-force re-derivation of every ground truth from the written corpus.

The generator's write log says where it planted each fact. This module ignores the write
log's *claims* and recomputes the answers from the text on disk, then compares. If the
two disagree, the corpus is wrong and the run fails — that is the difference between
synthetic data and made-up data.

Each check has an id (V1..V10) that the design doc and the tests both reference.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from .model import MONTHS
from .textindex import Corpus
from .textindex import load_jsonl
from .textindex import phrase_pattern


@dataclass
class ValidationReport:
    """Outcome of a full validation pass."""

    checks: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing failed."""
        return not self.failures

    def record(self, check: str, failure: str | None = None) -> None:
        """Count one executed check and record its failure message, if any."""
        self.checks[check] = self.checks.get(check, 0) + 1
        if failure:
            self.failures.append(f"{check}: {failure}")


def validate_corpus(corpus_dir: Path) -> ValidationReport:
    """Run V1-V10 over a generated corpus directory.

    Args:
        corpus_dir: Directory written by :func:`corpus.build_corpus`.

    Returns:
        A :class:`ValidationReport`; ``ok`` is False if any check failed.
    """
    corpus_dir = Path(corpus_dir)
    corpus = Corpus.load(corpus_dir)
    queries = {q["query_id"]: q for q in load_jsonl(corpus_dir / "queries.jsonl")}
    facts = load_jsonl(corpus_dir / "facts.jsonl")
    report = ValidationReport()

    _check_anchor_non_containment(facts, report)
    _check_fact_placement(corpus, facts, queries, report)
    _check_query_shapes(corpus, queries, report)
    _check_aggregation(corpus, queries, facts, report)
    _check_qrels(corpus_dir, corpus, queries, report)
    return report


def _fact_anchors(facts: list[dict]) -> list[str]:
    return sorted({f["anchor"] for f in facts})


def _check_anchor_non_containment(facts: list[dict], report: ValidationReport) -> None:
    """V9 — no anchor matches inside another anchor under the same boundary rules.

    If it did, "this anchor occurs in exactly one file" would be unprovable: a count of
    anchor A would silently include occurrences of the longer anchor B that contains it.
    """
    anchors = _fact_anchors(facts)
    index: dict[str, list[str]] = {}
    from .textindex import tokenize

    for anchor in anchors:
        for token in set(tokenize(anchor)):
            index.setdefault(token, []).append(anchor)
    for anchor in anchors:
        tokens = set(tokenize(anchor))
        candidates: set[str] = set()
        if tokens:
            postings = [set(index.get(t, [])) for t in tokens]
            candidates = set.intersection(*postings) if postings else set()
        pattern = phrase_pattern(anchor)
        # One check per anchor, not per candidate pair: with disjoint namespaces the
        # candidate set is usually empty, and a per-pair counter would report "0 checks
        # run" — indistinguishable from a detector that matches nothing.
        inside = [o for o in sorted(candidates - {anchor}) if pattern.search(o)]
        report.record(
            "V9-anchor-non-containment",
            None if not inside else f"anchor {anchor!r} occurs inside {inside[:3]}",
        )


def _check_fact_placement(
    corpus: Corpus, facts: list[dict], queries: dict, report: ValidationReport
) -> None:
    """V1/V2 — anchors occur only where planted, and in the declared turn."""
    by_anchor: dict[str, list[dict]] = {}
    for fact in facts:
        by_anchor.setdefault(fact["anchor"], []).append(fact)
    for anchor, rows in sorted(by_anchor.items()):
        expected = {r["file_uuid"] for r in rows}
        found = set(corpus.find_phrase(anchor))
        extra = sorted(found - expected)
        missing = sorted(expected - found)
        failure = None
        if extra or missing:
            failure = (
                f"anchor {anchor!r} planted in {sorted(expected)} but found in {sorted(found)}"
            )
        report.record("V1-anchor-exclusivity", failure)
        for row in rows:
            placed = corpus.turn_contains(row["file_uuid"], row["answer_turn"], anchor)
            report.record(
                "V2-anchor-in-declared-turn",
                None if placed else f"{anchor!r} absent from turn {row['answer_turn']}",
            )
            query = queries[row["query_id"]]
            spans = query["gold_turns"].get(row["file_uuid"], [])
            in_span = any(lo <= row["answer_turn"] <= hi for lo, hi in spans)
            report.record(
                "V8-gold-span-covers-answer",
                None if in_span else f"{row['query_id']} span {spans} misses {row['answer_turn']}",
            )


def _check_query_shapes(corpus: Corpus, queries: dict, report: ValidationReport) -> None:
    """V3/V6/V10 — per-class structural invariants."""
    series_members: dict[str, set[str]] = {}
    for doc in corpus.docs.values():
        series_members.setdefault(doc.series_id, set()).add(doc.file_uuid)
    for query in queries.values():
        gold = query["gold_files"]
        report.record(
            "V10-gold-non-empty",
            None if gold else f"{query['query_id']} has no gold files",
        )
        unknown = sorted(set(gold) - set(corpus.docs))
        report.record(
            "V10-gold-files-exist",
            None if not unknown else f"{query['query_id']} cites unknown files {unknown}",
        )
        if query["query_class"] == "multi_file":
            _check_multi_file(corpus, query, report)
        if query["query_class"] == "summarize":
            expected = sorted(series_members.get(query["series_id"], set()))
            failure = None
            if sorted(gold) != expected:
                failure = f"{query['query_id']} gold != series membership"
            report.record("V6-summarize-covers-series", failure)


def _check_multi_file(corpus: Corpus, query: dict, report: ValidationReport) -> None:
    """V3 — N components, N distinct files, and no file answers on its own."""
    components = query["components"]
    files = [c["file_uuid"] for c in components]
    failure = None
    if len(components) < 2:
        failure = f"{query['query_id']} has {len(components)} components"
    elif len(set(files)) != len(files):
        failure = f"{query['query_id']} plants two components in one file"
    elif sorted(set(files)) != sorted(query["gold_files"]):
        failure = f"{query['query_id']} gold != component files"
    report.record("V3-multifile-disjoint", failure)
    for file_uuid in sorted(set(files)):
        present = [
            c["aspect"]
            for c in components
            if phrase_pattern(c["anchor"]).search(corpus.docs[file_uuid].text)
        ]
        report.record(
            "V3-no-single-file-suffices",
            None if len(present) == 1 else f"{file_uuid} carries {len(present)} components",
        )


def _check_aggregation(
    corpus: Corpus, queries: dict, facts: list[dict], report: ValidationReport
) -> None:
    """V4/V5/V7 — every aggregation answer recomputed from the text and the rosters."""
    phrase_by_query: dict[str, str] = {}
    for fact in facts:
        if fact["kind"] in ("marker", "event"):
            phrase_by_query[fact["query_id"]] = fact["anchor"]
    for query in queries.values():
        if query["query_class"] != "aggregation":
            continue
        rule = query["rule"]
        if rule == "R6-agg-speaker-top":
            _check_speaker_top(corpus, query, report)
            continue
        phrase = phrase_by_query.get(query["query_id"])
        if phrase is None:
            report.record("V4-aggregation-exact", f"{query['query_id']} planted nothing")
            continue
        hits = corpus.find_phrase(phrase)
        if rule == "R3-agg-count-files":
            actual: object = len(hits)
        elif rule == "R4-agg-list-files":
            actual = sorted(hits)
        elif rule == "R5-agg-count-events":
            actual = sum(hits.values())
        elif rule == "R7-agg-temporal-count":
            month_key = _month_key_from_text(query["text"])
            actual = sum(1 for f in hits if corpus.docs[f].date.startswith(month_key))
        else:
            report.record("V4-aggregation-exact", f"unknown rule {rule}")
            continue
        failure = None
        if actual != query["answer"]:
            failure = (
                f"{query['query_id']} ({rule}) recorded {query['answer']!r}, text says {actual!r}"
            )
        report.record("V4-aggregation-exact", failure)


def _month_key_from_text(text: str) -> str:
    """Recover ``YYYY-MM`` from an R7 question, so the filter is re-derived not trusted."""
    for i, name in enumerate(MONTHS, start=1):
        marker = f" in {name} "
        if marker in text:
            year = text.split(marker, 1)[1].split()[0]
            return f"{year}-{i:02d}"
    raise ValueError(f"no month in temporal query: {text!r}")


def _check_speaker_top(corpus: Corpus, query: dict, report: ValidationReport) -> None:
    """V5 — attendance recomputed from the meeting records, with a strict maximum."""
    tally: dict[str, int] = {}
    for file_uuid in query["gold_files"]:
        for name in corpus.docs[file_uuid].speakers:
            tally[name] = tally.get(name, 0) + 1
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    failure = None
    if len(ranked) < 2 or ranked[0][1] == ranked[1][1]:
        failure = f"{query['query_id']} has a tied maximum: {ranked[:2]}"
    elif ranked[0][0] != query["answer"]["speaker"] or ranked[0][1] != query["answer"]["sessions"]:
        failure = f"{query['query_id']} recorded {query['answer']} but rosters say {ranked[0]}"
    report.record("V5-speaker-aggregation-exact", failure)


def _check_qrels(corpus_dir: Path, corpus: Corpus, queries: dict, report: ValidationReport) -> None:
    """V7 — the TREC qrels file is exactly the union of the queries' gold sets."""
    rows = [
        line.split("\t")
        for line in (corpus_dir / "qrels-files.tsv").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    from_file: dict[str, set[str]] = {}
    for qid, _zero, docid, _rel in rows:
        from_file.setdefault(qid, set()).add(docid)
    expected = {q["query_id"]: set(q["gold_files"]) for q in queries.values()}
    failure = None
    if from_file != expected:
        only_file = sorted(set(from_file) - set(expected))
        only_query = sorted(set(expected) - set(from_file))
        failure = f"qrels/query mismatch (qrels-only={only_file[:3]}, query-only={only_query[:3]})"
    report.record("V7-qrels-matches-queries", failure)
    unknown = sorted({d for docs in from_file.values() for d in docs} - set(corpus.docs))
    report.record("V7-qrels-docs-exist", None if not unknown else f"{len(unknown)} unknown docids")


def format_report(report: ValidationReport) -> str:
    """Render a report as a human-readable summary."""
    lines = [f"{'PASS' if report.ok else 'FAIL'} — {sum(report.checks.values())} checks"]
    for name, count in sorted(report.checks.items()):
        lines.append(f"  {name:34s} {count:7d}")
    for failure in report.failures[:25]:
        lines.append(f"  ! {failure}")
    if len(report.failures) > 25:
        lines.append(f"  ! ... and {len(report.failures) - 25} more")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m tests.eval.synthetic.validate <corpus-dir>``."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus_dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = validate_corpus(args.corpus_dir)
    if args.json:
        print(
            json.dumps(
                {"ok": report.ok, "checks": report.checks, "failures": report.failures}, indent=2
            )
        )
    else:
        print(format_report(report))
    return 0 if report.ok else 1
