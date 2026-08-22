"""#519 — the acceptance suite for the four query shapes a user actually asks.

The retrieval metrics say a *component* ranks well; this suite says whether
**the product works**, judged over a designated acceptance run's stored
artifacts (a `probe_chat_rag.py --out` results.json plus its label-judge
judgements). It asserts the two things #519 demands for every shape — the
answer against AMI's human annotations (via the #518-calibrated judge's
labels) and the deterministic coverage checks (#517) — plus the negative
controls and a latency ceiling.

**Every floor below is a RATCHET pinned to a measured value, not an
aspiration.** Basis run: ``ami81-postelitr-rerank`` + its qwen judgements
(2026-08-21, judge Kappa 0.857) — the arm whose configuration IS the shipped
default after #531 (final_chunks 40 / max_chunks_per_file 12 / rerank ON), so
the suite grades the product as shipped rather than a config nobody runs.
Measured there: multi-file offered coverage min 0.75 / full 22 of 25; judge
non-NONE 16/25 multi-file, 22/25 single-general, 3/6 due-outs, 4/11
speaker-scoped; all 6 negative controls declined. A floor set above today's
truth would make the suite permanently red (noise); a floor at
measured-minus-nothing breaks on the first regression, which is the job.
Raise floors when improvements land; NEVER lower one to make a run pass.
(The one prior re-pin followed that rule: the basis moved from the rerank-OFF
arm to this one because the SHIPPED config changed, and every floor was
re-derived from the new basis — due-outs rose 2/6 → 3/6.)

**Gating:** skips (with the producing commands) when the artifacts are
absent — they are gitignored, so CI always skips and a local run after a
probe+judge pass is the real audience. Point ``RAG_ACCEPTANCE_RUN`` /
``RAG_ACCEPTANCE_JUDGEMENTS`` at a different run to grade it instead.
"""

from __future__ import annotations

import json
import os
import statistics
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_RUN = Path(
    os.environ.get(
        "RAG_ACCEPTANCE_RUN", _REPO / ".rag-403" / "probe-runs" / "ami81-postelitr-rerank"
    )
)
_JUDGEMENTS = Path(
    os.environ.get(
        "RAG_ACCEPTANCE_JUDGEMENTS",
        _REPO / ".rag-403" / "labels" / "ami81-postelitr-rerank-judgements.jsonl",
    )
)

pytestmark = pytest.mark.skipif(
    not (_RUN / "results.json").is_file() or not _JUDGEMENTS.is_file(),
    reason=(
        f"acceptance artifacts absent ({_RUN}/results.json + {_JUDGEMENTS}) — produce them "
        "with scripts/probe_chat_rag.py --out ... and scripts/judge_chat_answers.py judge"
    ),
)

#: AMI's four scenario roles — the speaker axis shape 3 exercises.
_ROLES = ("Project Manager", "Industrial Designer", "User Interface", "Marketing")


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    loaded: list[dict] = json.loads((_RUN / "results.json").read_text(encoding="utf-8"))
    assert len(loaded) >= 81, "acceptance run is not a full question set"
    return loaded


@pytest.fixture(scope="module")
def judged() -> dict[str, dict]:
    out = {}
    with _JUDGEMENTS.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                out[row["item"]] = row
    assert out, "judgements file is empty"
    return out


def _shape(rows: list[dict], category: str) -> list[dict]:
    picked = [r for r in rows if r["category"] == category]
    assert picked, f"acceptance run carries no {category!r} items"
    return picked


# --------------------------------------------------------------- run integrity


class TestRunIntegrity:
    def test_every_turn_completed_without_error(self, rows):
        errored = [r["label"] for r in rows if r.get("error")]
        assert errored == []

    def test_every_judged_item_parsed_cleanly(self, rows, judged):
        degraded = [i for i, j in judged.items() if j.get("degraded")]
        assert degraded == [], "a Kappa/verdict over regex-fallback labels grades the regex"

    def test_latency_ceiling_per_shape(self, rows):
        """'Accurate' must not be bought with an unacceptable wait (#519). The
        ceiling is generous — it catches hangs, not tuning drift."""
        for category in ("single_specific", "single_general", "multi_file"):
            lat = [r["latency_s"] for r in _shape(rows, category) if r.get("latency_s")]
            assert lat, category
            assert statistics.median(lat) < 120, (category, statistics.median(lat))


# ---------------------------------------------- negative controls (not optional)


class TestNegativeControlsDecline:
    def test_all_controls_declined(self, rows, judged):
        """A confident answer about an absent topic/speaker is a failure, not a
        near-miss. FULL on a control is the judge's documented taxonomy edge —
        its marker reference SAYS a decline is correct, so FULL and REFUSED
        both mean 'correctly declined'; NONE/PARTIAL mean it invented content."""
        controls = _shape(rows, "negative_control")
        bad = [
            r["label"]
            for r in controls
            if judged[r["label"]]["judge_label"] not in ("REFUSED", "FULL")
        ]
        assert bad == []

    def test_controls_cite_nothing_from_the_scope(self, rows):
        controls = _shape(rows, "negative_control")
        leaked = [r["label"] for r in controls if r.get("files_consulted_uuids")]
        assert leaked == []


# ------------------------------------------------------- coverage (#517, deterministic)


class TestScopeCoverage:
    def test_map_coverage_is_complete_wherever_a_map_ran(self, rows):
        """The #517 metric: files_touched == files_in_scope, or a named reason.
        Ratchet basis: 52 of 81 turns carried a map on the basis run, every one
        complete."""
        import sys

        sys.path.insert(0, str(_REPO / "backend"))
        from tests.eval.harness.chat_instrumentation import extract_scope_coverage

        values = [extract_scope_coverage(r.get("msg_metadata") or {}) for r in rows]
        present = [v for v in values if v is not None]
        assert len(present) >= 40, "the map ran on far fewer turns than the basis run"
        incomplete = [v for v in present if v < 1.0]
        assert incomplete == []

    def test_offered_coverage_floor_on_multi_file(self, rows):
        """Retrieval must OFFER (nearly) the whole scope on the aggregation
        shapes. Ratchet basis: min 0.75, 22/25 full, mean 0.97."""
        multi = _shape(rows, "multi_file")
        coverage = []
        for r in multi:
            scope = set(r.get("scope_file_uuids") or [])
            offered = {c["file_uuid"] for c in (r.get("offered_citations") or [])}
            assert scope, r["label"]
            coverage.append(len(offered & scope) / len(scope))
        assert min(coverage) >= 0.75
        assert sum(1 for c in coverage if c == 1.0) >= 22


# ------------------------------------------------- the four shapes, judge-graded


def _non_none_fraction(items: list[dict], judged: dict[str, dict]) -> float:
    labels = [judged[r["label"]]["judge_label"] for r in items]
    return sum(1 for label in labels if label in ("FULL", "PARTIAL")) / len(labels)


class TestShapeAnswersCarryReferenceContent:
    """Judge labels against AMI's human annotations (#518, Kappa 0.857).

    PARTIAL counts: the system's measured failure mode is incompleteness, and
    the floors below pin how often an answer carries at least SOME of what the
    human annotator wrote. NONE/unwarranted-REFUSED are the failures.
    """

    def test_shape_1_summaries(self, rows, judged):
        """'What is the summary?' — single_general. Ratchet basis: 22/25 ≥ PARTIAL."""
        assert _non_none_fraction(_shape(rows, "single_general"), judged) >= 0.88

    def test_shape_2_and_4_cross_meeting_aggregation(self, rows, judged):
        """'Find me the due outs / decisions / problems across these meetings' —
        multi_file. Ratchet basis: 16/25 ≥ PARTIAL (0.64)."""
        assert _non_none_fraction(_shape(rows, "multi_file"), judged) >= 0.64

    def test_shape_2_due_outs_specifically(self, rows, judged):
        """The action-items slice of shape 2 — the product's headline ask, and
        its MEASURED WEAKEST slice: ratchet basis **3 of 6** ≥ PARTIAL
        (2026-08-21, shipped-config arm). That is a finding, not a target —
        #532's second defect (decisions/actions have no deterministic
        representation; the digest is topic-centrality, not artifact
        extraction) names the fix, and this floor rises when #532 arm (d)
        lands. Until then the floor only stops the slice getting WORSE."""
        due_outs = [r for r in rows if "action_items" in r["label"]]
        assert len(due_outs) >= 6
        assert _non_none_fraction(due_outs, judged) >= 0.50

    def test_shape_3_speaker_scoped(self, rows, judged):
        """'What did <role> say about <topic>?' — the single_specific items that
        name an AMI role. Ratchet basis **4 of 11** ≥ PARTIAL (2026-08-21,
        shipped-config arm) — the #523/#525 short-turn chain, whose measured
        cure (read-time context expansion) is the deferred #523 A/B. Raise
        this floor when #523 ships."""
        speaker_items = [
            r
            for r in _shape(rows, "single_specific")
            if any(role in r["question"] for role in _ROLES)
        ]
        assert len(speaker_items) >= 10
        assert _non_none_fraction(speaker_items, judged) >= 0.36
