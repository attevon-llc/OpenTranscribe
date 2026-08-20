"""Digest re-masking, and the hazard it exists to prevent (#403 Stage 4, #65).

**The first test here proves the bug, not the fix.** Routing a digest through
``mask_chunks`` returns MORE text than the digest contained — the whole time
span rebuilt verbatim — from a function whose name asserts it masked something.
That is worse than no masking, because nothing about a call to ``mask_*`` invites
a second look, and under the current redaction policy the path that would leak is
the one that egresses to a third-party provider.

So the guard is calibrated the way this repo's other detectors are: a
**must-fire** case that fails if the hazard ever stops being real, beside the
must-stay-clean case for the fix. If someone "simplifies" ``mask_digests`` into
an overload of ``mask_chunks``, the second test goes green and the first goes
red — which is the point.

The second contract difference is the fail-closed unit. A chunk fails closed
**whole**; a digest fails closed **per sentence**, keeping the sentences that did
mask. One function cannot hold both, which is the other reason these are two.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.chat.redactor import mask_chunks
from app.services.chat.redactor import mask_digests
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit

#: The digest section as the INDEX holds it: two selected sentences from
#: opposite ends of an hour-long recording.
DIGEST_TEXT = "We agreed the budget. We shipped on Friday."

#: Every segment inside that section's time range — i.e. what a time-range
#: rebuild would return in its place. It contains material the digest omits.
FULL_SPAN = [
    "We agreed the budget.",
    "Then Dana read out her card number 4111 1111 1111 1111.",
    "There was a long argument about the vendor.",
    "We shipped on Friday.",
]


@contextmanager
def _one_session(db):
    yield db


def _factory(db):
    """A ``session_scope``-shaped factory over one prepared session.

    Both maskers take the FACTORY, never a ``Session`` (issue #83): they gather
    their cached spans, close the transaction, and only then mask — the inline
    fallback below runs Presidio, whose cold build is ~10 s.
    """
    return lambda: _one_session(db)


def _digest_hit(content: str = DIGEST_TEXT) -> ChunkHit:
    return ChunkHit(
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=5,
        chunk_index=-1,
        content=content,
        title="Weekly sync",
        speaker=None,
        start_time=12.5,
        end_time=3400.0,
        digest_section=0,
    )


def _cfg(
    *,
    enabled: bool = True,
    redact_before_llm: bool = True,
    categories=("pii", "profanity", "custom"),
):
    """Stand-in for ``EffectiveRedactionConfig``; ``enabled_categories`` is read
    by the inline fallback and so must be present here too."""
    return SimpleNamespace(
        enabled=enabled,
        redact_before_llm=redact_before_llm,
        enabled_categories=set(categories),
    )


def _segment(seg_id: int, text: str):
    return SimpleNamespace(
        id=seg_id, text=text, redactions=[], words=None, start_time=float(seg_id)
    )


def _sentence(text: str, segment_ids: list[int], kind: str = "segment_ids"):
    return {
        "text": text,
        "order": 0,
        "speaker": "Dana",
        "provenance": {
            "kind": kind,
            "segment_ids": segment_ids,
            "start_time": 12.5,
            "end_time": 20.0,
        },
    }


def _facts_db(sentences, *, status="completed", segment_batches=None):
    """A session answering the status probe, the facts read and the segment reads.

    ``segment_batches`` is consumed **in order**, one batch per sentence lookup,
    rather than being filtered by id. A MagicMock cannot evaluate an ``IN``
    clause, and faking one badly would make the test pass on a masker that
    ignored provenance entirely — which is the thing under test. Ordered batches
    keep the fake honest about what it does and does not prove: it exercises the
    per-sentence loop and the fail-closed branches, and says nothing about the
    query. The query is covered by the live ``--answerer product`` run.
    """
    from app.core import constants as C  # noqa: N812

    resolved_status = C.REDACTION_STATUS_DONE if status == "completed" else status
    batches = list(segment_batches or [])

    db = MagicMock()
    digest_payload = {"sections": [{"index": 0, "sentences": sentences}]}

    # *targets, not one: the scan probe selects four columns (id, status,
    # redaction_coverage, language) so it can answer the v391 coverage gate as
    # well as the status check. A single-arg signature raises TypeError inside
    # the caller's try/except and every section is withheld — a stub failure
    # that reads exactly like the fail-closed behaviour under test.
    def _query(*targets):
        # Each target stringified SEPARATELY. `str(targets)` on the tuple calls
        # repr() on its elements, and InstrumentedAttribute has no custom repr —
        # so the key becomes "<sqlalchemy...object at 0x7f...>" and matches
        # nothing. Every probe then fell to the segment branch and the caller
        # reported "no cached provenance", which is indistinguishable from the
        # fail-closed path it was supposed to be testing.
        key = " ".join(str(t) for t in targets)
        result = MagicMock()
        if "redaction_status" in key:
            result.filter.return_value.scalar.return_value = resolved_status
            result.filter.return_value.first.return_value = SimpleNamespace(
                id=1,
                redaction_status=resolved_status,
                redaction_coverage=None,  # pre-v391 row: trusted, so the gate stays open
                language="en",
            )
        elif "digest" in key:
            result.filter.return_value.first.return_value = (digest_payload,)
            # `_gather_digest_plans` now reads every hit's digest through ONE
            # batched `file_id IN (...)` query (`_load_digest_rows`) instead of
            # a per-hit `_digest_sentences` call (W2.1 amendment b). Every hit
            # in this module uses `_digest_hit()`'s default `file_id=5`, so a
            # single-row `.all()` answer covers every existing test here.
            result.filter.return_value.all.return_value = [(5, digest_payload)]
        else:
            ordered = result.filter.return_value.order_by.return_value
            ordered.all.return_value = batches.pop(0) if batches else []
        return result

    db.query.side_effect = _query
    return db


# --------------------------------------------------------------- the hazard


def test_the_chunk_path_over_discloses_a_digest():
    """MUST-FIRE. If this ever passes, `mask_digests` has stopped being needed.

    `mask_chunks` rebuilds from every segment overlapping the hit's time range.
    For a digest that is the whole recording, so the "masked" output is LONGER
    than the input and contains sentences the digest deliberately left out.
    """
    segments = {n: _segment(n, text) for n, text in enumerate(FULL_SPAN, start=1)}
    db = MagicMock()
    status_q = MagicMock()
    from app.core import constants as C  # noqa: N812

    status_q.filter.return_value.scalar.return_value = C.REDACTION_STATUS_DONE
    status_q.filter.return_value.first.return_value = SimpleNamespace(
        id=1,
        redaction_status=C.REDACTION_STATUS_DONE,
        redaction_coverage=None,  # pre-v391 row: trusted, so the coverage gate stays open
        language="en",
    )
    seg_q = MagicMock()
    seg_q.filter.return_value.order_by.return_value.all.return_value = list(segments.values())
    db.query.side_effect = [status_q, seg_q]

    with (
        patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg()),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=lambda text, *_a, **_k: (text, []),
        ),
    ):
        wrong = mask_chunks(_factory(db), [_digest_hit()], user_id=1)

    assert len(wrong[0].content) > len(DIGEST_TEXT), (
        "the hazard is gone — a digest through the chunk path no longer "
        "over-discloses, so re-derive whether mask_digests is still required"
    )
    assert "card number" in wrong[0].content, "it returned the whole span verbatim"


def test_the_digest_path_returns_only_the_digest_sentences():
    """The must-stay-clean twin of the test above."""
    db = _facts_db(
        [_sentence("We agreed the budget.", [1]), _sentence("We shipped on Friday.", [4])],
        segment_batches=[
            [_segment(1, "We agreed the budget.")],
            [_segment(4, "We shipped on Friday.")],
        ],
    )

    with (
        patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg()),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=lambda text, *_a, **_k: (text, []),
        ),
    ):
        masked = mask_digests(_factory(db), [_digest_hit()], user_id=1)

    assert "card number" not in masked[0].content
    assert len(masked[0].content) <= len(DIGEST_TEXT) + 1


# ------------------------------------------------------------- fail closed


def test_an_unresolvable_config_withholds_every_digest():
    db = MagicMock()
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        side_effect=RuntimeError("no policy"),
    ):
        masked = mask_digests(_factory(db), [_digest_hit()], user_id=1)

    assert masked[0].content == ""
    assert masked[0].was_masked is True


def test_an_unknown_provenance_kind_contributes_nothing():
    """A char_range sentence (#362) reaching a transcript masker is a bug, not a hint."""
    db = _facts_db([_sentence("We agreed the budget.", [1], kind="char_range")])

    with patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg()):
        masked = mask_digests(_factory(db), [_digest_hit()], user_id=1)

    assert masked[0].content == ""


def test_one_unmaskable_sentence_does_not_discard_the_others():
    """The per-sentence contract: a chunk fails closed whole, a digest does not."""
    db = _facts_db(
        [
            _sentence("We agreed the budget.", [1]),
            _sentence("We shipped on Friday.", []),  # no provenance ids -> unusable
        ],
        segment_batches=[[_segment(1, "We agreed the budget.")]],
    )

    with (
        patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg()),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=lambda text, *_a, **_k: (text, []),
        ),
    ):
        masked = mask_digests(_factory(db), [_digest_hit()], user_id=1)

    assert masked[0].content == "We agreed the budget."


def test_detection_not_finished_falls_back_to_inline_and_never_to_raw():
    db = _facts_db([_sentence("x", [1])], status="processing")

    with (
        patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg()),
        patch("app.services.chat.redactor._mask_inline", return_value="[MASKED]") as inline,
    ):
        masked = mask_digests(_factory(db), [_digest_hit()], user_id=1)

    assert inline.called
    assert masked[0].content == "[MASKED]"


def test_the_inline_fallback_withholds_a_section_on_a_swallowed_detector_failure():
    """The digest path reaches the SAME inline masker, so it had the same hole.

    A section whose provenance is unresolvable is masked inline, and
    ``detect_segment_spans`` swallowing a PII-detector error there returned the
    rendered section verbatim while ``mask_digests`` marked it masked. Nothing
    here is patched at ``_mask_inline`` — patching it would prove only that the
    fallback is called, not that it is safe.
    """

    def _detect(_text, _words, _det_cfg, *, failures=None, **_kwargs):
        if failures is not None:
            failures.append("pii")
        return [], None

    db = _facts_db([_sentence("x", [1])], status="processing")

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(categories=("pii",)),
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            side_effect=_detect,
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=lambda text, *_a, **_k: (text, []),
        ),
    ):
        masked = mask_digests(_factory(db), [_digest_hit()], user_id=1)

    assert masked[0].content == ""
    assert "We agreed the budget" not in masked[0].content


def test_the_per_sentence_path_needs_no_detector_at_all():
    """Provenance masking reads CACHED spans, so it has no sink to forget.

    Stated as a test because "does `mask_digests` have the same hole?" is
    answerable two ways: the inline fallback did (above), the per-sentence path
    never could — it runs no detector. A detector call appearing here later would
    be a new egress surface and this goes red.
    """
    detect_calls: list[str] = []

    db = _facts_db(
        [_sentence("We agreed the budget.", [1])],
        segment_batches=[[_segment(1, "We agreed the budget.")]],
    )

    def _record_detect_call(text, *_a, **_k):
        # A named function rather than `detect_calls.append(text) or (...)`:
        # append returns None, so the `or` idiom works but reads as though the
        # append produced a value, and mypy rejects it as func-returns-value.
        detect_calls.append(text)
        return ([], None)

    with (
        patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg()),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            side_effect=_record_detect_call,
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=lambda text, *_a, **_k: (text, []),
        ),
    ):
        masked = mask_digests(_factory(db), [_digest_hit()], user_id=1)

    assert masked[0].content == "We agreed the budget."
    assert detect_calls == []


def test_policy_off_passes_the_digest_through_untouched():
    db = MagicMock()
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(redact_before_llm=False),
    ):
        masked = mask_digests(_factory(db), [_digest_hit()], user_id=1)

    assert masked[0].content == DIGEST_TEXT
    assert masked[0].was_masked is False


def test_no_digests_is_not_a_database_round_trip():
    db = MagicMock()
    opened = []

    def _factory_that_counts():
        opened.append(1)
        return _one_session(db)

    assert mask_digests(_factory_that_counts, [], user_id=1) == []
    assert db.query.called is False
    assert opened == [], "an empty digest list must not even open a session"


def test_the_digest_inline_fallback_runs_with_no_db_session_open():
    """#83: the same gather-then-close contract as ``mask_chunks``.

    A section whose provenance cannot be resolved is masked inline, which builds
    a Presidio ``AnalyzerEngine`` — ~10 s cold, measured at 13.9 s ``idle in
    transaction`` when it ran inside the gather session. A plain SELECT holds
    ACCESS SHARE for the life of its transaction, so that queues every
    ``ALTER TABLE`` behind a chat turn.
    """
    live = 0
    live_during_detection: list[int] = []

    db = _facts_db([_sentence("x", [1])], status="processing")  # -> inline fallback

    @contextmanager
    def _counting_scope():
        nonlocal live
        live += 1
        try:
            yield db
        finally:
            live -= 1

    def _detect(_text, _words, _det_cfg, **_kwargs):
        live_during_detection.append(live)
        return [], None

    with (
        patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg()),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            side_effect=_detect,
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            return_value=("[MASKED]", []),
        ),
    ):
        masked = mask_digests(_counting_scope, [_digest_hit()], user_id=1)

    assert live_during_detection == [0], (
        "a DB session was live while the digest inline detector ran "
        f"(observed: {live_during_detection})"
    )
    # The control: it still masked, rather than being emptied by a missing session.
    assert masked[0].content == "[MASKED]"


# ------------------------------------------------------------ G7 citations


def test_a_digest_citation_declares_its_kind_and_names_no_speaker():
    """Addendum G7: a digest rendered as a quote attributes words nobody said."""
    from app.services.chat.citations import build_citation
    from app.services.chat.redactor import MaskedChunk

    citation = build_citation(1, MaskedChunk(source=_digest_hit(), content=DIGEST_TEXT))

    assert citation["kind"] == "digest"
    assert citation["speaker"] is None
    assert citation["digest_section"] == 0


def test_a_digest_citation_carries_the_sections_real_start_time():
    """`start_time=0` would deep-link every summary citation to 0:00."""
    from app.services.chat.citations import build_citation
    from app.services.chat.redactor import MaskedChunk

    citation = build_citation(1, MaskedChunk(source=_digest_hit(), content=DIGEST_TEXT))
    assert citation["start_time"] == 12.5


def test_a_chunk_citation_is_unchanged_and_still_names_its_speaker():
    from app.services.chat.citations import build_citation
    from app.services.chat.redactor import MaskedChunk

    chunk = ChunkHit(
        file_uuid="f",
        file_id=1,
        chunk_index=3,
        content="hello",
        speaker="Dana",
        start_time=30.0,
    )
    citation = build_citation(2, MaskedChunk(source=chunk, content="hello"))

    assert citation["kind"] == "chunk"
    assert citation["speaker"] == "Dana"
    assert citation["digest_section"] is None


# ------------------------------------------------------ the digest hit itself


def test_is_digest_is_an_explicit_field_not_a_sign_test():
    """The negative chunk_index is an index-sort detail, not a public contract."""
    assert _digest_hit().is_digest is True
    assert ChunkHit(file_uuid="f", file_id=1, chunk_index=0, content="x").is_digest is False


def test_the_digest_flag_survives_the_retrieval_cache_round_trip():
    original = _digest_hit()
    restored = ChunkHit.from_cache_dict(original.to_cache_dict())

    assert restored.is_digest is True
    assert restored.digest_section == 0
    assert restored.start_time == original.start_time


def test_section_zero_resolves_because_zero_is_falsy_not_absent():
    """MEASURED REGRESSION: `chunk.digest_section or -1` made 0 falsy.

    Section 0 is the FIRST section of every digest, so the bug sent every
    leading section down the inline fallback — masking would still have been
    applied, but by re-detecting rather than from cached spans, silently and on
    the request path.
    """
    from app.services.chat.redactor import _digest_sentences

    db = _facts_db([_sentence("We agreed the budget.", [1])])
    found = _digest_sentences(db, _digest_hit())

    assert found is not None, "section 0 must resolve like any other section"
    assert len(found) == 1
