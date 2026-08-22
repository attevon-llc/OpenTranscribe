"""``mapreduce.coverage`` (issue #63) — does the map's output equal the resolved scope?

The bug this module exists to catch, restated from ``services/chat/CLAUDE.md``: asked
for a summary over a 25-file scope, the RANKED digest leg (``retrieve_digests``)
returned 50 sections drawn from **8 files**, because sections cluster by relevance. The
composed block was headed "recordings: 8" and the model answered confidently over a
scope of 25 — and no eval framework catches it, because every claim in that answer
genuinely WAS grounded in the 8 files it saw (RAGAS/DeepEval score groundedness against
whatever context was retrieved, not against the scope that was asked for).

``file_summaries.scope_digest_hits`` already fixed the underlying defect by reading
``file_facts`` for every file in a bounded scope directly instead of ranking. What this
module adds is the reconciliation step nothing previously performed: given the resolved
scope and the map's own ``(hits, coverage)`` output, does every scoped file show up
either as a hit or as a COUNTED, NAMED reason it does not? A caller that regresses to
the ranked leg — a future code path forgetting to call ``scope_digest_hits`` at all, a
mock that silently drops rows — produces exactly the bug shape above, and this module
is what would fail a test over it instead of everyone trusting the block's own header.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from app.services.chat.mapreduce import ScopeCoverageError
from app.services.chat.mapreduce import assert_full_coverage
from app.services.chat.mapreduce import check_scope_coverage
from app.services.chat.mapreduce import scope_digest_hits

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Hit:
    """The only attribute :func:`check_scope_coverage` reads off a hit."""

    file_uuid: str


def _hits(*uuids: str) -> list[_Hit]:
    return [_Hit(uuid) for uuid in uuids]


# --------------------------------------------------------------------------- #
# The headline shape: a scope of N files where the map saw fewer.
# --------------------------------------------------------------------------- #


def test_full_coverage_is_complete():
    scope = [f"uuid-{n}" for n in range(25)]
    result = check_scope_coverage(scope, _hits(*scope), {"files_without_artifacts": 0})

    assert result.applicable is True
    assert result.complete is True
    assert result.scope_size == 25
    assert result.files_missing == frozenset()
    assert result.files_out_of_scope == frozenset()


def test_the_headline_bug_shape_partial_coverage_with_no_accounting_fails():
    """25 in scope, only 8 touched, and NOTHING on the coverage dict explains the
    other 17 — the exact "recordings: 8" defect, reproduced as a set, not a count."""
    scope = [f"uuid-{n}" for n in range(25)]
    touched = scope[:8]

    result = check_scope_coverage(scope, _hits(*touched), coverage={})

    assert result.applicable is True
    assert result.complete is False
    assert len(result.files_missing) == 17
    assert result.files_missing == frozenset(scope[8:])
    assert result.unaccounted == 17
    assert "17" in result.reason


def test_the_headline_bug_shape_passes_once_the_gap_is_named():
    """The SAME 8-of-25 shortfall, but the map's own coverage dict says why: the
    other 17 have no `file_facts` row yet. Complete, because a named reason for
    every gap is what 'complete' means here — not that every file has content."""
    scope = [f"uuid-{n}" for n in range(25)]
    touched = scope[:8]

    result = check_scope_coverage(scope, _hits(*touched), coverage={"files_without_artifacts": 17})

    assert result.complete is True
    assert result.unaccounted == 0


def _scope_db(rows):
    """Same helper shape as ``test_chat_mapreduce.py``: two chained `.filter()`s."""
    db = MagicMock()
    query = db.query.return_value.outerjoin.return_value.filter.return_value
    query.filter.return_value.all.return_value = rows
    return db


def _facts_row(file_id: int, uuid: str, title: str, sections: int = 1):
    digest = {
        "sections": [
            {"index": i, "text": f"Section {i} of {title}.", "start_time": i * 60.0}
            for i in range(sections)
        ]
    }
    return (file_id, uuid, title, digest)


def test_the_real_scope_map_is_complete_over_a_25_file_scope():
    """Drives the REAL ``scope_digest_hits`` (the fix), not a stand-in, and feeds
    its real output straight into the checker — the two halves of #63 wired
    together exactly as a caller would use them."""
    scope = [f"uuid-{n}" for n in range(25)]
    rows = [_facts_row(n, uuid, f"Recording {n}") for n, uuid in enumerate(scope)]

    hits = scope_digest_hits(_scope_db(rows), scope)
    result = check_scope_coverage(scope, hits, hits.coverage)

    assert result.complete is True
    assert result.scope_size == 25
    assert len(result.files_touched) == 25


def test_the_ranked_leg_misused_as_the_map_step_fails_the_check():
    """If a future call site regresses to the RANKED digest leg — the exact
    historical mistake — as the map step, this is what catches it: the ranked
    leg's hits cluster into 8 files and carry no ``coverage`` dict at all (it is
    not the map's output, it never claimed to cover anything)."""
    scope = [f"uuid-{n}" for n in range(25)]
    ranked_leg_hits = _hits(*scope[:8])  # relevance clustered into 8 of 25

    result = check_scope_coverage(scope, ranked_leg_hits, coverage=None)

    assert result.complete is False
    assert len(result.files_missing) == 17


# --------------------------------------------------------------------------- #
# file_uuids=None ("all accessible") vs file_uuids=[] ("match nothing") —
# a security invariant, not just a coverage one (inverting these leaks the
# whole library — services/chat/CLAUDE.md).
# --------------------------------------------------------------------------- #


def test_file_uuids_none_is_unbounded_and_not_applicable():
    """Mapping an unbounded scope is not possible — there is no enumerated list
    to check — so this must refuse to grade it rather than report a fabricated
    100% or 0%."""
    result = check_scope_coverage(None, _hits("uuid-1", "uuid-2"), coverage={})

    assert result.applicable is False
    assert result.scope_size is None
    assert result.complete is None
    assert result.reason  # names WHY, not just that it declined


def test_file_uuids_empty_list_is_bounded_and_trivially_complete():
    """`[]` means 'match nothing' — a real, gradeable, degenerate scope, not the
    same thing as `None` at all."""
    result = check_scope_coverage([], [], coverage={})

    assert result.applicable is True
    assert result.scope_size == 0
    assert result.complete is True


def test_a_hit_against_an_empty_scope_is_a_leak_not_coverage():
    """The inverted-semantics failure mode made concrete: if `file_uuids=[]` were
    ever (mis)treated as 'match everything', a hit would appear here despite an
    empty scope. That must fail the check, not pass it."""
    result = check_scope_coverage([], _hits("uuid-leaked"), coverage={})

    assert result.applicable is True
    assert result.complete is False
    assert result.files_out_of_scope == frozenset({"uuid-leaked"})


def test_none_and_empty_scope_are_handled_oppositely():
    """Assert BOTH directions in one place: `None` never claims completeness (it
    refuses to grade at all) and `[]` never claims inapplicability (it grades,
    and trivially passes when nothing leaked)."""
    none_result = check_scope_coverage(None, [], coverage={})
    empty_result = check_scope_coverage([], [], coverage={})

    assert none_result.applicable is False
    assert empty_result.applicable is True
    assert none_result.complete is None
    assert empty_result.complete is True


# --------------------------------------------------------------------------- #
# Single-file scope.
# --------------------------------------------------------------------------- #


def test_single_file_scope_touched_is_complete():
    result = check_scope_coverage(["uuid-1"], _hits("uuid-1"), coverage={})
    assert result.complete is True
    assert result.files_touched == frozenset({"uuid-1"})


def test_single_file_scope_missing_and_unaccounted_is_incomplete():
    result = check_scope_coverage(["uuid-1"], [], coverage={})
    assert result.complete is False
    assert result.files_missing == frozenset({"uuid-1"})


# --------------------------------------------------------------------------- #
# "Looked and found nothing" vs "never looked" — the distinction the brief
# names explicitly, driven through the REAL scope_digest_hits output.
# --------------------------------------------------------------------------- #


def test_no_content_and_no_artifacts_are_distinguished_by_key_but_both_account_for_the_gap():
    """Three files: one touched, one never had a `file_facts` row at all (never
    consulted), one has a digest but zero sections (consulted, contributed
    nothing). `scope_digest_hits` tracks the second two under DIFFERENT keys —
    this asserts both are honoured as legitimate, separately-named reasons, not
    that they collapse into one undifferentiated "missing" bucket."""
    scope = ["touched", "missing-row", "empty-digest"]
    rows = [
        _facts_row(1, "touched", "Has content"),
        (2, "empty-digest", "No sections", {"sections": []}),
        # "missing-row" is absent from `rows` entirely — outer join finds no row.
    ]
    hits = scope_digest_hits(_scope_db(rows), scope)

    assert hits.coverage["files_without_artifacts"] == 1  # missing-row
    assert hits.coverage["files_no_content"] == 1  # empty-digest

    result = check_scope_coverage(scope, hits, hits.coverage)

    assert result.complete is True
    assert result.files_missing == frozenset({"missing-row", "empty-digest"})
    assert result.accounted_gap == 2


def test_dropping_the_coverage_dict_turns_an_explained_gap_into_an_unexplained_one():
    """Same map output as above, but a caller that discards `.coverage` (reads
    only `len(hits)`, say) loses the distinction entirely — proving the
    ACCOUNTING, not just the existence of two files with no hit, is what makes
    them tellable apart. Without it both files look identical: no hit, no
    reason on record."""
    scope = ["touched", "missing-row", "empty-digest"]
    rows = [
        _facts_row(1, "touched", "Has content"),
        (2, "empty-digest", "No sections", {"sections": []}),
    ]
    hits = scope_digest_hits(_scope_db(rows), scope)

    result = check_scope_coverage(scope, hits, coverage=None)

    assert result.complete is False
    assert result.unaccounted == 2


# --------------------------------------------------------------------------- #
# Fail-closed accounting: an unrecognised key cannot manufacture coverage, and
# an over-claiming coverage dict is flagged rather than hidden.
# --------------------------------------------------------------------------- #


def test_an_unrecognised_coverage_key_does_not_count_as_accounted():
    """A misspelled or made-up key must not silently grant coverage — only the
    two named, documented reasons do."""
    result = check_scope_coverage(
        ["uuid-1"], [], coverage={"some_other_reason_nobody_asked_for": 1}
    )
    assert result.complete is False
    assert result.accounted_gap == 0


def test_an_over_claiming_coverage_dict_is_flagged_not_silently_clipped():
    """`files_without_artifacts` claims MORE gap than files are actually missing
    — a bookkeeping bug in the caller. Must not silently clip to "complete"."""
    result = check_scope_coverage(
        ["uuid-1"], _hits("uuid-1"), coverage={"files_without_artifacts": 5}
    )
    assert result.complete is False
    assert result.unaccounted == -5


def test_duplicate_scope_uuids_are_deduplicated():
    result = check_scope_coverage(["uuid-1", "uuid-1", "uuid-2"], _hits("uuid-1", "uuid-2"), {})
    assert result.scope_size == 2
    assert result.complete is True


# --------------------------------------------------------------------------- #
# The hard-assertion form.
# --------------------------------------------------------------------------- #


def test_assert_full_coverage_raises_naming_the_real_missing_uuids():
    scope = [f"uuid-{n}" for n in range(25)]
    with pytest.raises(ScopeCoverageError) as excinfo:
        assert_full_coverage(scope, _hits(*scope[:8]), coverage={})
    message = str(excinfo.value)
    assert "17" in message
    for missing_uuid in scope[8:]:
        assert missing_uuid in message


def test_assert_full_coverage_is_silent_when_complete():
    scope = ["uuid-1", "uuid-2"]
    result = assert_full_coverage(scope, _hits(*scope), coverage={})
    assert result.complete is True


def test_assert_full_coverage_is_silent_for_an_unbounded_scope():
    """Not applicable is not the defect this guards against — a genuinely
    unbounded scope must not raise just because nothing was checkable."""
    result = assert_full_coverage(None, _hits("uuid-1"), coverage={})
    assert result.applicable is False
