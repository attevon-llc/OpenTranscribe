"""The query-trace seam must be free, unfailable, and unable to leak.

The panel itself is GH #514 and is not built. These tests pin the three
properties that make it safe to instrument the pipeline NOW, ahead of it — if any
one of them is false, adding trace calls to retrieval code is a liability rather
than groundwork.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from app.services import chat as chat_pkg
from app.services.chat.trace import NULL_RECORDER
from app.services.chat.trace import ListTraceRecorder
from app.services.chat.trace import Outcome
from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceEvent
from app.services.chat.trace import emit
from tests.helpers import does_not_raise


class _ExplodingRecorder:
    """A recorder that fails on every call, as a broken implementation would."""

    def record(self, event: TraceEvent) -> None:
        raise RuntimeError("recorder is broken")


def test_a_broken_recorder_cannot_break_a_turn():
    """Design rule 1: the trace reports, it never participates.

    A trace bug taking down chat would be strictly worse than having no trace.
    """
    with does_not_raise("a recorder implementation that raises must not break the turn it traces"):
        emit(_ExplodingRecorder(), QueryStage.FOUND, count=12)


def test_no_recorder_is_a_no_op():
    with does_not_raise("`emit(None, ...)` is the no-recorder-attached call shape every seam uses"):
        emit(None, QueryStage.SUBMITTED)
        NULL_RECORDER.record(TraceEvent(stage=QueryStage.SUBMITTED))


def test_events_are_recorded_in_order_with_their_outcome():
    rec = ListTraceRecorder()
    emit(rec, QueryStage.SUBMITTED)
    emit(rec, QueryStage.FANNED_VECTOR, plane="chunk", source="opensearch")
    emit(rec, QueryStage.FOUND, Outcome.EMPTY, count=0)

    assert [e.stage for e in rec.events] == [
        QueryStage.SUBMITTED,
        QueryStage.FANNED_VECTOR,
        QueryStage.FOUND,
    ]
    assert rec.events[1].detail == {"plane": "chunk", "source": "opensearch"}
    assert rec.events[2].outcome is Outcome.EMPTY


def test_empty_and_skipped_are_distinguishable():
    """ "Looked and found nothing" must never render the same as "never looked".

    This is the ambiguity the panel exists to remove, so it has to survive at the
    data layer or the UI cannot express it.
    """
    rec = ListTraceRecorder()
    emit(rec, QueryStage.FOUND, Outcome.EMPTY, count=0)
    emit(rec, QueryStage.RERANKED, Outcome.SKIPPED, reason="disabled")
    assert [e.outcome for e in rec.events] == [Outcome.EMPTY, Outcome.SKIPPED]


@pytest.mark.parametrize(
    "leaky_key, leaky_value",
    [
        ("title", "Q3 Board Review with Alice Chen"),
        ("speaker", "Alice Chen"),
        ("filename", "acquisition-call.mp4"),
        ("file_uuid", "019f294e-967a-7000-918d-b5dee9659565"),
        ("query", "what did alice say about the layoffs"),
    ],
)
def test_identifying_detail_is_dropped_not_recorded(leaky_key: str, leaky_value: str):
    """The trace must not HOLD permission-scoped data, not merely avoid showing it.

    Node labels are the obvious leak surface for this feature: a file title or a
    speaker name is scoped to who may see that recording. The safe design is that
    the trace never carries one, so no rendering mistake downstream can expose it.
    """
    rec = ListTraceRecorder()
    # `Any`, not `object`: `emit` has keyword-only `parent`/`node_id` typed
    # `str | None`, and mypy matches a `**dict[str, object]` splat against them.
    detail: dict[str, Any] = {"count": 3, leaky_key: leaky_value}
    emit(rec, QueryStage.FOUND, outcome=Outcome.OK, **detail)

    assert len(rec.events) == 1, "the event itself should still be recorded"
    recorded = rec.events[0].detail
    assert leaky_key not in recorded, f"{leaky_key!r} reached the trace"
    assert leaky_value not in str(recorded), f"{leaky_value!r} reached the trace"
    assert recorded == {"count": 3}, "allowlisted detail must survive the scrub"


def test_the_recorder_is_bounded_and_says_so_when_it_truncates():
    """A pathological fan-out must not grow a turn's memory without limit.

    And truncation must be visible: a silently shortened trace is a trace that
    lies about what ran, which is the failure mode this feature exists to fix.
    """
    rec = ListTraceRecorder(cap=5)
    for _ in range(50):
        emit(rec, QueryStage.FOUND, count=1)

    assert len(rec.events) == 5
    assert rec.truncated is True

    unbounded = ListTraceRecorder(cap=5)
    emit(unbounded, QueryStage.FOUND, count=1)
    assert unbounded.truncated is False, "control: truncation must not be always-on"


def test_fan_out_siblings_can_be_related_to_a_parent():
    """Parallel legs are the reason this is a tree rather than a list."""
    rec = ListTraceRecorder()
    emit(rec, QueryStage.PLANNED, node_id="plan", legs=3)
    for leg in range(3):
        emit(rec, QueryStage.FANNED_VECTOR, parent="plan", node_id=f"leg{leg}", leg=leg)

    children = [e for e in rec.events if e.parent == "plan"]
    assert len(children) == 3
    assert {e.detail["leg"] for e in children} == {0, 1, 2}


def test_every_stage_in_the_documented_workflow_exists():
    """The vocabulary is a contract with the UI; a rename must fail loudly here."""
    expected = {
        "submitted",
        "validated",
        "parsed_names",
        "rewritten",
        "cache_lookup",
        "planned",
        "fanned_relational",
        "fanned_vector",
        "found",
        "reranked",
        "sampled",
        "expanded",
        "filtered",
        "budgeted",
        "reviewed",
        "presented",
    }
    assert {s.value for s in QueryStage} == expected


def _scan_emitted(sources: Iterable[tuple[str, str]]) -> set[str]:
    """``QueryStage`` members passed positionally to an ``emit``/``emit_trace``.

    Takes (filename, source) pairs rather than reading the package itself, so
    the control below can feed it a module it fully controls. A scanner tested
    only against the real tree cannot be shown to detect anything.
    """
    emitters = {"emit", "emit_trace"}
    found: set[str] = set()
    for filename, source in sources:
        tree = ast.parse(source, filename=filename)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in emitters:
                continue
            for arg in node.args:
                if (
                    isinstance(arg, ast.Attribute)
                    and isinstance(arg.value, ast.Name)
                    and arg.value.id == "QueryStage"
                ):
                    found.add(arg.attr)
    return found


def _chat_package_sources() -> list[tuple[str, str]]:
    package = Path(chat_pkg.__file__).parent
    return [(str(p), p.read_text(encoding="utf-8")) for p in sorted(package.glob("*.py"))]


def test_every_stage_in_the_vocabulary_has_an_emitter():
    """A stage nothing ever emits is a row the panel can never render.

    ``QueryStage`` is a promise about what a turn reports. ``EXPANDED`` was
    defined, documented, given a label in all twelve locales, and emitted by
    NOTHING — so read-time context expansion was silently absent from every
    trace while the whole suite stayed green. Only a screenshot caught it,
    because counting rendered rows is the one check that notices an omission
    rather than a mistake.

    Static rather than "drive a turn and look": a behavioural test cannot see a
    stage that only fires on a flag it did not set, which is the exact shape of
    the defect.
    """
    missing = {s.name for s in QueryStage} - _scan_emitted(_chat_package_sources())
    assert not missing, f"stages defined but never emitted: {sorted(missing)}"


def test_the_emit_scanner_reports_a_stage_that_nothing_emits():
    """Must-fire control: the scanner detects absence, rather than never firing.

    A detector that matches nothing reports a clean sweep, which is
    indistinguishable from full coverage — the failure mode the repo's own
    auditors exist to prevent. Two synthetic modules, identical but for one
    emit, must produce different answers.
    """
    covered = "emit(rec, QueryStage.SUBMITTED)\nemit_trace(r, QueryStage.EXPANDED, count=1)\n"
    uncovered = "emit(rec, QueryStage.SUBMITTED)\n"

    assert _scan_emitted([("covered.py", covered)]) == {"SUBMITTED", "EXPANDED"}
    assert _scan_emitted([("uncovered.py", uncovered)]) == {"SUBMITTED"}


def test_a_configured_limit_is_carryable_but_content_still_is_not():
    """``limit`` carries a CONFIGURED bound, never anything derived from content.

    It exists so "48 -> 12, max 4 per file" and "9 of 12 excerpts fit" are
    reportable. The paired negative is the point: widening the allowlist for a
    number must not widen it for a string that happens to sit beside one.
    """
    rec = ListTraceRecorder()
    emit(rec, QueryStage.SAMPLED, kept=12, dropped=36, limit=4, title="Q3 Board Review")

    recorded = rec.events[0].detail
    assert recorded == {"kept": 12, "dropped": 36, "limit": 4}
    assert "title" not in recorded, "an identifying key must still be scrubbed"
