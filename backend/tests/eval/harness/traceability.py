"""Deterministic traceability metrics for a live chat-RAG probe turn (task #11 / GH #463).

The user's own framing for what an evaluation must report: **representation, accuracy,
correctness — with traceability to the data.** Of those, this module builds exactly one
column, split out because it is the one that is measurable TODAY without a judge:

| property | measurable now? | where |
|---|---|---|
| representation (did the answer draw on the whole scope?) | yes, deterministic | ``chat_instrumentation.extract_scope_coverage``, ``mapreduce/coverage.py`` |
| **traceability** (does every citation resolve to a real, in-scope chunk that was actually in the prompt?) | yes, deterministic | **this module** |
| correctness / accuracy (does the answer match the human reference?) | **no** — needs an LLM judge, and the judge is not calibrated (GH #518) | not built here, deliberately |

**This module never scores whether an answer is right.** It scores whether the answer's
*citation apparatus* is honest: every ``[n]`` marker points somewhere real, everything the
turn points the reader at is inside the scope they asked for, the citations rendered match
what the model was actually given, and a quoted claim is not a fabricated quote. A model can
pass every measure here and still be wrong about the facts — that is why this stays
"traceability", never "accuracy" or "groundedness".

Four measures, each answering a narrower question than "is this answer grounded", built from
one raw probe record (``scripts/probe_chat_rag.py``'s ``Result``/``result_to_record`` shape,
same as :mod:`tests.eval.harness.probe_metrics`):

1. **``citation_resolution``** — every ``[n]`` marker in the answer must resolve to a
   citation the turn actually rendered. A marker that resolves to nothing is a DANGLING
   reference: the reader clicks a source number that names nothing. Computed from
   ``record["app_answer"]`` (regex over the literal markers) against the ``id`` field of
   ``record["citations"]`` — the PERSISTED citations, which ``citations.extract_used_citations``
   already builds by intersecting the answer's markers with the offered set. That intersection
   is exactly the set this check needs: a marker is "resolved" iff it produced an entry in
   ``citations``, so checking against ``citations`` ids is equivalent to checking against the
   full offered set for this specific question, with no probe change required.
2. **``citation_validity``** — every citation the turn renders must name a file inside the
   conversation's resolved scope (``record["scope_file_uuids"]``). A citation whose file sits
   outside scope is a LEAK: retrieval reached past the boundary the user set, not a formatting
   slip. Checked against ``record["offered_citations"]`` when the probe captured it (the full
   ``sources`` SSE frame, stripped to ``id``/``file_uuid`` — see
   ``scripts/probe_chat_rag.py::_offered_citation_refs``), because that is everything the model
   was shown, not only what the answer went on to cite. Records captured before that field
   existed fall back to ``record["citations"]`` (the rendered/used subset) — a real but narrower
   check, since a leaked-but-uncited excerpt still reached the model and just never produced a
   citation. "Exists in the index" is NOT independently re-verified here: a citation can only
   name an excerpt id assigned to a chunk that was actually retrieved
   (``citations.build_citation``), so that half is already guaranteed by construction, not by a
   second lookup this module would need a live index to perform.
3. **``prompt_membership``** — the issue #384 invariant, per turn instead of only in a unit
   test: ``chunks_used`` (``msg_metadata``) must equal the number of citations OFFERED in the
   ``sources`` SSE frame (``test_chat_sources_frame.py``'s
   ``len(offered_citations) == chunks_used`` pin). Requires
   ``scripts/probe_chat_rag.py`` to have captured that frame — a record from before that change
   reports ``None`` here, never a fabricated match.
4. **``quote_fidelity``** — of the answer's ``[n]`` markers that are IMMEDIATELY preceded by a
   quoted span (``"...text..."[n]``), what fraction of those quotes appear, verbatim
   (casefolded, whitespace-collapsed), in the CITED citation's ``snippet``. Reported as
   ``quote_fidelity``, never as "groundedness" — an unquoted claim is not measured by this at
   all (most of an answer is unquoted prose, and none of it is checked here), and a quote's
   true home in the source chunk can extend past the ~240-char snippet ``citations._snippet``
   keeps, so a real quote can still score unsupported if it starts before or ends after that
   window. This is a proxy for one narrow failure mode (a fabricated quotation attributed to a
   real citation), not a substitute for a correctness judge.

Every extractor below reads ``app_answer`` / a citation's ``snippet`` PURELY internally, to
compute a count or a ratio — the text itself is never assigned to an output field.
:func:`tests.eval.harness.probe_metrics.assert_no_prose` (re-used, not re-implemented) is the
second, independent guard: it walks the assembled artifact and refuses to serialise it if any
forbidden key appears anywhere, so a future field added by copy-paste still cannot leak text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from tests.eval.harness.probe_metrics import assert_no_prose
from tests.eval.harness.report import dumps as _dumps_canonical

__all__ = [
    "TurnTraceability",
    "assert_no_prose",
    "build_traceability_results",
    "build_traceability_rows",
    "dumps",
    "extract_turn_traceability",
    "render_traceability_table",
    "summarize_traceability_rows",
]

#: Same convention as ``app/services/chat/citations.py``'s ``_CITATION_RE`` — a bare
#: ``[n]`` marker, 1-3 digits. Kept as its own compiled pattern rather than importing the
#: app module, so this eval-only package never depends on ``app.*`` at import time.
_CITATION_RE = re.compile(r"\[(\d{1,3})\]")

#: A double-quoted span immediately followed by a citation marker, e.g.
#: ``"we should ship friday"[3]``. The 3-character floor drops trivial/empty quotes
#: (``""[1]``) that would otherwise vacuously "match" any snippet via substring
#: containment.
_QUOTED_CITATION_RE = re.compile(r'"([^"]{3,})"\s*\[(\d{1,3})\]')

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Casefold and collapse whitespace — the same tolerance ``answers.normalise_name``
    applies to a name, used here for quote comparison."""
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def _rate(total: int, bad: int) -> float | None:
    """``(total - bad) / total``, or ``None`` when there was nothing to measure.

    ``total <= 0`` means no opportunity for this check to apply this turn — reporting
    a rate of 0.0 or 1.0 would read as a measurement that never happened (the same
    "absent is not zero" rule ``chat_instrumentation`` and ``probe_metrics.coverage_ratio``
    already follow).
    """
    if total <= 0:
        return None
    return (total - bad) / total


def _citation_resolution_counts(answer: str, citations: list[dict[str, Any]]) -> tuple[int, int]:
    """``(markers_total, markers_dangling)`` — every ``[n]`` occurrence in ``answer``,
    and how many do not match a rendered citation's ``id``."""
    citation_ids = {
        int(citation["id"])
        for citation in citations
        if isinstance(citation, dict) and "id" in citation
    }
    markers = [int(match.group(1)) for match in _CITATION_RE.finditer(answer)]
    dangling = sum(1 for marker in markers if marker not in citation_ids)
    return len(markers), dangling


def _citation_validity_source(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The citations to scope-check: the full OFFERED set when the probe captured it,
    else the narrower rendered/used set. See the module docstring, measure 2."""
    offered = record.get("offered_citations")
    if offered is not None:
        return list(offered)
    return list(record.get("citations") or [])


def _citation_validity_counts(
    citations: list[dict[str, Any]], scope_file_uuids: list[str]
) -> tuple[int, int | None]:
    """``(citations_total, citations_leaked)``.

    ``citations_leaked`` is ``None`` when the scope itself is empty/unresolved (0 files)
    — a leak cannot be distinguished from "no scope was set", the same ambiguity
    :func:`tests.eval.harness.probe_metrics.coverage_ratio` refuses to resolve as a
    measured zero.
    """
    total = len(citations)
    scope = {str(uuid) for uuid in scope_file_uuids}
    if not scope:
        return total, None
    leaked = sum(
        1
        for citation in citations
        if isinstance(citation, dict) and str(citation.get("file_uuid")) not in scope
    )
    return total, leaked


def _quote_fidelity_counts(answer: str, citations: list[dict[str, Any]]) -> tuple[int, int]:
    """``(quotes_total, quotes_unsupported)`` — see module docstring, measure 4.

    Matched against ``record["citations"]`` (never ``offered_citations``, which is
    deliberately stripped down to ``id``/``file_uuid`` and carries no ``snippet`` to
    check a quote against).
    """
    by_id = {
        int(citation["id"]): str(citation.get("snippet") or "")
        for citation in citations
        if isinstance(citation, dict) and "id" in citation
    }
    total = 0
    unsupported = 0
    for match in _QUOTED_CITATION_RE.finditer(answer):
        quote_text, marker_text = match.group(1), match.group(2)
        total += 1
        snippet = by_id.get(int(marker_text))
        if snippet is None or _normalise(quote_text) not in _normalise(snippet):
            unsupported += 1
    return total, unsupported


@dataclass(frozen=True)
class TurnTraceability:
    """One chat turn's deterministic traceability record. Every field is a count, a
    ratio, a bool, or ``None`` for "not measured" — never text the app or a human wrote.

    Attributes:
        query_id: The probe's own label for this turn.
        category: The probe's query-shape bucket.
        markers_total: Total ``[n]`` occurrences in the answer (repeats counted).
        markers_dangling: Of those, how many resolve to no rendered citation.
        citation_resolution_rate: See :func:`_rate`; ``None`` when the answer cited
            nothing at all.
        citations_total: Citations checked for scope validity (offered set, or the
            rendered/used fallback — see the module docstring).
        citations_leaked: Of those, how many name a file outside the resolved scope.
            ``None`` when the scope itself is empty/unresolved.
        citation_validity_rate: See :func:`_rate`; ``None`` when ``citations_leaked``
            is ``None`` or there was nothing to check.
        chunks_used: ``msg_metadata.chunks_used``, or ``None`` if unmeasured.
        offered_citations_count: ``len(offered_citations)`` from the ``sources`` SSE
            frame, or ``None`` on a record captured before the probe carried it.
        prompt_membership_matches: ``offered_citations_count == chunks_used`` — the
            issue #384 invariant, or ``None`` when either side is unmeasured.
        quotes_total: ``[n]`` markers immediately preceded by a quoted span.
        quotes_unsupported: Of those, how many quotes are absent from their cited
            citation's snippet.
        quote_fidelity: See :func:`_rate`; ``None`` when the answer made no quoted
            claims. NOT a groundedness score — see the module docstring.
    """

    query_id: str
    category: str
    markers_total: int
    markers_dangling: int
    citation_resolution_rate: float | None
    citations_total: int
    citations_leaked: int | None
    citation_validity_rate: float | None
    chunks_used: int | None
    offered_citations_count: int | None
    prompt_membership_matches: bool | None
    quotes_total: int
    quotes_unsupported: int
    quote_fidelity: float | None

    def as_json(self) -> dict[str, Any]:
        """JSON-safe, deterministic form. Field order matches the dataclass."""
        return {
            "query_id": self.query_id,
            "category": self.category,
            "markers_total": self.markers_total,
            "markers_dangling": self.markers_dangling,
            "citation_resolution_rate": self.citation_resolution_rate,
            "citations_total": self.citations_total,
            "citations_leaked": self.citations_leaked,
            "citation_validity_rate": self.citation_validity_rate,
            "chunks_used": self.chunks_used,
            "offered_citations_count": self.offered_citations_count,
            "prompt_membership_matches": self.prompt_membership_matches,
            "quotes_total": self.quotes_total,
            "quotes_unsupported": self.quotes_unsupported,
            "quote_fidelity": self.quote_fidelity,
        }


def extract_turn_traceability(record: dict[str, Any]) -> TurnTraceability:
    """Build one turn's traceability record from a raw probe result.

    Reads ``app_answer`` and a citation's ``snippet`` only to compute counts/ratios —
    neither is ever assigned to an output field. See :class:`TurnTraceability` for what
    every field means.

    Args:
        record: One raw per-question probe result (``scripts/probe_chat_rag.py``'s
            ``result_to_record`` shape).

    Returns:
        This turn's traceability record.

    Raises:
        KeyError: ``record`` is missing ``"label"`` or ``"category"``.
    """
    answer = str(record.get("app_answer") or "")
    rendered_citations = record.get("citations") or []
    scope_file_uuids = record.get("scope_file_uuids") or []

    markers_total, markers_dangling = _citation_resolution_counts(answer, rendered_citations)

    validity_citations = _citation_validity_source(record)
    citations_total, citations_leaked = _citation_validity_counts(
        validity_citations, scope_file_uuids
    )

    chunks_used = record.get("chunks_used")
    offered = record.get("offered_citations")
    offered_citations_count = None if offered is None else len(offered)
    prompt_membership_matches = (
        None
        if offered_citations_count is None or chunks_used is None
        else offered_citations_count == int(chunks_used)
    )

    quotes_total, quotes_unsupported = _quote_fidelity_counts(answer, rendered_citations)

    return TurnTraceability(
        query_id=str(record["label"]),
        category=str(record["category"]),
        markers_total=markers_total,
        markers_dangling=markers_dangling,
        citation_resolution_rate=_rate(markers_total, markers_dangling),
        citations_total=citations_total,
        citations_leaked=citations_leaked,
        citation_validity_rate=(
            None if citations_leaked is None else _rate(citations_total, citations_leaked)
        ),
        chunks_used=chunks_used,
        offered_citations_count=offered_citations_count,
        prompt_membership_matches=prompt_membership_matches,
        quotes_total=quotes_total,
        quotes_unsupported=quotes_unsupported,
        quote_fidelity=_rate(quotes_total, quotes_unsupported),
    )


def build_traceability_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One :class:`TurnTraceability` per record, sorted by ``query_id`` — same
    determinism rule as :func:`tests.eval.harness.probe_metrics.build_probe_rows`."""
    rows = [extract_turn_traceability(record) for record in records]
    return [row.as_json() for row in sorted(rows, key=lambda row: row.query_id)]


#: Ratio fields summarised uniformly below — mean AND min, never mean alone (a corpus
#: mean hides the one turn that cited outside scope).
_RATIO_FIELDS = ("citation_resolution_rate", "citation_validity_rate", "quote_fidelity")


def summarize_traceability_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-category rollup: for each ratio field, ``coverage``/``total`` plus, only over
    the MEASURED subset, ``mean`` AND ``min``. ``prompt_membership_matches`` reports a
    boolean ``rate`` instead, following ``chat_instrumentation.summarize_instrumentation``.

    Args:
        rows: :class:`TurnTraceability.as_json` dicts, e.g. from
            :func:`build_traceability_rows`.

    Returns:
        A dict keyed by category. A field with zero measured rows in a category reports
        only ``coverage: 0`` — never a fabricated 0.0/1.0.
    """
    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_category.setdefault(row["category"], []).append(row)

    summary: dict[str, Any] = {}
    for category in sorted(by_category):
        members = by_category[category]
        entry: dict[str, Any] = {"queries": len(members)}

        for field_name in _RATIO_FIELDS:
            values = [row[field_name] for row in members if row[field_name] is not None]
            field_entry: dict[str, Any] = {"coverage": len(values), "total": len(members)}
            if values:
                field_entry["mean"] = round(sum(values) / len(values), 6)
                field_entry["min"] = min(values)
            entry[field_name] = field_entry

        matches = [
            row["prompt_membership_matches"]
            for row in members
            if row["prompt_membership_matches"] is not None
        ]
        match_entry: dict[str, Any] = {"coverage": len(matches), "total": len(members)}
        if matches:
            match_entry["rate"] = sum(1 for value in matches if value) / len(matches)
        entry["prompt_membership_matches"] = match_entry

        summary[category] = entry
    return summary


def build_traceability_results(
    *,
    run_name: str,
    target: dict[str, Any],
    records: list[dict[str, Any]],
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """The committed, metrics-only traceability artifact. Deterministic by construction.

    Args:
        run_name: A short identifier for this run (e.g. the baseline directory name).
        target: Which stack/model config was probed, structural terms only — see
            :func:`tests.eval.harness.probe_metrics.build_probe_results`.
        records: Raw per-question probe results.
        notes: Free-text notes ABOUT THE MEASUREMENT — never question/answer text.
            Checked by :func:`tests.eval.harness.probe_metrics.assert_no_prose` like
            everything else in the artifact.

    Returns:
        The results document, already validated by ``assert_no_prose``.

    Raises:
        tests.eval.harness.probe_metrics.ProseLeakError: A forbidden field reached the
            artifact.
    """
    rows = build_traceability_rows(records)
    results: dict[str, Any] = {
        "schema_version": 1,
        "run_name": run_name,
        "target": target,
        "licence_note": (
            "Metrics only. No question text, reference answers, app answer prose, or "
            "citation snippets are recorded here — the source text is read internally to "
            "compute counts/ratios and is never assigned to an output field. See the "
            "module docstrings of tests.eval.harness.probe_metrics (issue #72) and "
            "tests.eval.harness.traceability (task #11 / GH #463)."
        ),
        "scope_note": (
            "Traceability, not correctness. citation_resolution / citation_validity / "
            "prompt_membership are structural checks against what the turn rendered and "
            "was offered. quote_fidelity checks only EXPLICITLY QUOTED spans against a "
            "~240-char citation snippet and is not a groundedness or accuracy score. "
            "Whether the answer's claims are actually correct needs an LLM judge, which "
            "is not calibrated yet (GH #518) and is deliberately not reported here."
        ),
        "rows": rows,
        "summary": summarize_traceability_rows(rows),
        "notes": list(notes or []),
    }
    assert_no_prose(results)
    return results


def render_traceability_table(rows: list[dict[str, Any]]) -> str:
    """The per-turn traceability table, as GitHub-flavoured Markdown."""
    header = [
        "query_id",
        "category",
        "citation_resolution",
        "citation_validity",
        "prompt_membership",
        "quote_fidelity",
    ]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(header)) + "|",
    ]

    def _ratio(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.4f}"

    def _bool(value: bool | None) -> str:
        return "n/a" if value is None else str(value)

    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    row["query_id"],
                    row["category"],
                    _ratio(row["citation_resolution_rate"]),
                    _ratio(row["citation_validity_rate"]),
                    _bool(row["prompt_membership_matches"]),
                    _ratio(row["quote_fidelity"]),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def dumps(results: dict[str, Any]) -> str:
    """Canonical JSON: sorted keys, fixed separators, trailing newline. Delegates to
    ``harness.report.dumps`` — one JSON convention across this whole package."""
    return _dumps_canonical(results)
