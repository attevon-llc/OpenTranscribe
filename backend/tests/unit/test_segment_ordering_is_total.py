"""Segment reads must impose a TOTAL order, or re-indexing is not reproducible.

``ORDER BY start_time`` looks sufficient and is not. Overlapping speech and
interpolated backchannels routinely share an onset — measured on the QMSum eval
corpus: **3,072 tie groups covering 6,152 segments**. Postgres is free to return
tied rows in any order, and in practice returns them in physical storage order,
which a delete-then-bulk-insert reshuffles.

That is not a theoretical concern. It was found by re-indexing one unchanged
corpus three times and getting three different chunk counts (119,950 / 119,949 /
120,540) and three different nDCG@10 values (0.1052 / 0.1023 / 0.1029). The
mechanism: tied segments swap, speaker-turn grouping changes, chunk boundaries
move. A 10 ms ``"Uh - huh ."`` backchannel landing before or after the 1.25 s
utterance it overlaps decides whether the turn is split.

The consequence is worse than a wobbly benchmark. #403's D5 compares every stage
against the previous one as control, and Stage 3 mandates a full reindex — so an
unstable index would let a stage pass its gate on reshuffling alone.

This test pins the indexing path, which is the one that feeds retrieval. The
other ~20 single-column ``order_by(TranscriptSegment.start_time)`` sites are
tracked separately; several are user-visible (subtitle ordering, the transcript
the UI renders, what summarisation shows the model).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEXING_TASK = REPO_ROOT / "backend" / "app" / "tasks" / "search_indexing_task.py"

pytestmark = pytest.mark.skipif(
    not INDEXING_TASK.exists(), reason="search_indexing_task.py not present in this checkout"
)


def _order_by_args(source: str) -> list[list[str]]:
    """Every ``.order_by(...)`` call's arguments, rendered as dotted names."""
    tree = ast.parse(source)
    calls: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "order_by":
            continue
        rendered: list[str] = []
        for arg in node.args:
            parts: list[str] = []
            cur: ast.AST = arg
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            rendered.append(".".join(reversed(parts)))
        calls.append(rendered)
    return calls


def _segment_order_by(source: str) -> list[list[str]]:
    return [
        args
        for args in _order_by_args(source)
        if any(a.startswith("TranscriptSegment.") for a in args)
    ]


def test_the_indexing_read_orders_by_more_than_start_time():
    """The tie-breaker is what makes re-indexing reproducible."""
    calls = _segment_order_by(INDEXING_TASK.read_text(encoding="utf-8"))
    assert calls, "expected search_indexing_task.py to order its TranscriptSegment read"

    for args in calls:
        assert args[0] == "TranscriptSegment.start_time", (
            f"chronological order is the primary key of this read; got {args}"
        )
        assert len(args) > 1, (
            "ORDER BY start_time alone is not a total order — 6,152 segments in the "
            "eval corpus share an onset, and tied rows come back in physical order, "
            f"which a re-insert reshuffles. Add a tie-breaker. Got: {args}"
        )
        assert args[-1] == "TranscriptSegment.id", (
            "the FINAL sort key must be the primary key: start_time and end_time can "
            f"both tie (identical spans with different text). Got: {args}"
        )


def test_no_module_anywhere_orders_segments_by_start_time_alone():
    """Repo-wide: the defect was in 23 places, not one (issue #433).

    Several were user-visible rather than benchmark noise — subtitle cue order,
    the transcript the UI renders, what summarisation shows the model. Pinning
    only the indexing path would leave those free to regress, and a partial fix
    to a systemic defect is the shape that gets quietly undone.
    """
    app_dir = REPO_ROOT / "backend" / "app"
    offenders: list[str] = []
    scanned = 0
    # No try/except around the read or the parse: a file under backend/app that
    # cannot be read or cannot be parsed is a real problem, and swallowing it
    # here would silently shrink the scan — the exact way a detector comes to
    # report a clean tree it never actually looked at.
    for path in sorted(app_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "TranscriptSegment" not in source:
            continue
        scanned += 1
        calls = _segment_order_by(source)
        for args in calls:
            # A descending or otherwise deliberate ordering is out of scope; this
            # detector is only about chronological reads that forgot a tie-break.
            if (
                args
                and args[0] == "TranscriptSegment.start_time"
                and args[-1] != ("TranscriptSegment.id")
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {args}")

    assert not offenders, (
        "chronological segment reads must end in the primary key, or tied rows "
        "come back in physical storage order and a re-insert silently reorders "
        "the transcript:\n  " + "\n  ".join(offenders)
    )


def test_the_scanner_would_notice_a_regression():
    """Guard the guard: prove the detector fires on the shape it exists to catch.

    Without this, a scanner that silently matched nothing would report a clean
    file — indistinguishable from a correct one, which is the failure mode the
    repo's test auditors exist to prevent.
    """
    regressed = "q.order_by(TranscriptSegment.start_time).all()"
    calls = _segment_order_by(regressed)
    assert calls == [["TranscriptSegment.start_time"]]
    assert len(calls[0]) == 1, "the single-key shape must be detectable"

    fixed = (
        "q.order_by(TranscriptSegment.start_time, TranscriptSegment.end_time,"
        " TranscriptSegment.id).all()"
    )
    fixed_calls = _segment_order_by(fixed)
    assert fixed_calls[0][-1] == "TranscriptSegment.id"
    assert len(fixed_calls[0]) == 3
