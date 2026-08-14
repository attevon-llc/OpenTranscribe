"""Chunk re-masking before the LLM (issue #52).

The OpenSearch chunk index stores transcript text UNREDACTED, so these tests
guard the one place that stops redacted content from reaching a third-party
provider. The controlling property is fail-CLOSED: when we cannot establish that
text is safe, we send nothing rather than sending it raw.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from app.services.chat.redactor import mask_chunks
from app.services.search.chunk_retrieval import ChunkHit


def _chunk(content: str = "my number is 555-1234") -> ChunkHit:
    return ChunkHit(
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=5,
        chunk_index=0,
        content=content,
        title="Call",
        speaker="Dana",
        start_time=10.0,
        end_time=40.0,
    )


def _cfg(*, enabled: bool, redact_before_llm: bool, categories=("pii", "profanity", "custom")):
    """A stand-in for ``EffectiveRedactionConfig``.

    ``enabled_categories`` is part of the real dataclass and the inline path
    reads it, so omitting it here would let a masker that ignores the user's
    categories pass — and the whole narrowness contract lives in that field.
    """
    return SimpleNamespace(
        enabled=enabled,
        redact_before_llm=redact_before_llm,
        enabled_categories=set(categories),
    )


def _detector_that_swallows(name: str = "pii"):
    """``detect_segment_spans`` behaving EXACTLY as it does on a detector error.

    It catches the exception, returns the spans it did collect (here: none), and
    records the detector name in the ``failures`` sink. Raising instead would
    test a failure mode the real function does not have — which is why a
    happy-path or an exception-based test could never see this defect.
    """

    def _detect(_text, _words, _det_cfg, *, failures=None, **_kwargs):
        if failures is not None:
            failures.append(name)
        return [], None

    return _detect


def test_policy_off_passes_content_through_untouched():
    db = MagicMock()
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=True, redact_before_llm=False),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert masked[0].content == "my number is 555-1234"
    assert masked[0].was_masked is False


def test_redaction_disabled_entirely_passes_content_through():
    db = MagicMock()
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=False, redact_before_llm=True),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert masked[0].was_masked is False


def _db_with(status, segments, coverage=None, language="en"):
    """A db mock that answers the scan probe and the segment query distinctly.

    ``coverage`` is what ``media_file.redaction_coverage`` holds (v391): the
    detectors the finished scan actually ran. ``None`` is a pre-v391 row, which
    :func:`~app.services.redaction.coverage.uncovered_detectors` trusts on
    purpose — so a test that wants the coverage gate to BITE must pass a list
    that omits the detector, not leave this defaulted.
    """
    db = MagicMock()
    scan_q = MagicMock()
    # Both accessors, deliberately: the pre-coverage code read `.scalar()` for the
    # status alone and the current code reads `.first()` for the whole scan row.
    # Answering only one couples the fixture to an implementation detail, and a
    # red run against the older code then fails on the fixture rather than on the
    # behaviour under test — which is indistinguishable from real evidence.
    scan_q.filter.return_value.scalar.return_value = status
    scan_q.filter.return_value.first.return_value = SimpleNamespace(
        id=1,
        redaction_status=status,
        redaction_coverage=coverage,
        language=language,
    )
    seg_q = MagicMock()
    seg_q.filter.return_value.order_by.return_value.all.return_value = segments
    db.query.side_effect = [scan_q, seg_q]
    return db


def test_policy_on_rebuilds_text_from_cached_segment_spans():
    """Primary path: cached spans make masking sub-millisecond, no detectors run."""
    segment = SimpleNamespace(
        text="my number is 555-1234",
        redactions=[{"char_start": 13, "char_end": 21, "category": "pii", "entity_type": "PHONE"}],
        words=None,
    )
    db = _db_with("done", [segment])

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            return_value=("my number is [PHONE]", []),
        ) as mask_segment,
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert masked[0].content == "my number is [PHONE]"
    assert masked[0].was_masked is True
    assert "555-1234" not in masked[0].content
    # The REAL masker ran against the cached spans — not a stubbed helper.
    assert mask_segment.call_args[0][1] == segment.redactions


def test_uncached_spans_do_not_pass_as_masked():
    """Regression: the fail-OPEN that shipped in the first implementation.

    A file whose redaction detection never ran has ``redactions`` NULL. The old
    code still took the "cached spans" branch, applied nothing, and returned the
    RAW text while marking it masked. Chat is exactly where you query files you
    never opened, so this was the common case.
    """
    segment = SimpleNamespace(text="my number is 555-1234", redactions=None, words=None)
    db = _db_with(None, [segment])  # redaction_status not 'done'

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            return_value=([], None),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            return_value=("my number is [PHONE]", []),
        ),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    # Must have gone down the inline-detection path, not returned raw text.
    assert "555-1234" not in masked[0].content


def test_incomplete_detection_status_forces_the_inline_path():
    """Any status other than 'done' means the cached spans cannot be trusted."""
    segment = SimpleNamespace(text="secret 555-1234", redactions=[], words=None)

    for status in (None, "pending", "processing", "failed"):
        db = _db_with(status, [segment])
        with (
            patch(
                "app.services.redaction.config.resolve_effective_config",
                return_value=_cfg(enabled=True, redact_before_llm=True),
            ),
            patch(
                "app.services.redaction.service.RedactionService.detect_segment_spans",
                return_value=([], None),
            ) as detect,
            patch(
                "app.services.redaction.service.RedactionService.mask_segment",
                return_value=("[MASKED]", []),
            ),
        ):
            mask_chunks(db, [_chunk()], user_id=1)
        assert detect.called, f"status={status!r} should force inline detection"


def test_masking_error_on_the_cached_path_fails_closed():
    """An exception mid-mask must withhold the chunk, not leak the original."""
    segment = SimpleNamespace(text="my number is 555-1234", redactions=[], words=None)
    db = _db_with("done", [segment])

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=RuntimeError("masker exploded"),
        ),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert masked[0].content == ""
    assert masked[0].was_masked is True


def test_falls_back_to_inline_masking_when_segments_are_missing():
    """Files whose detection hasn't finished still get masked, just more slowly."""
    db = _db_with("done", [])

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            return_value=([], None),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            return_value=("my number is [PHONE]", []),
        ),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert masked[0].content == "my number is [PHONE]"
    assert masked[0].was_masked is True


def test_inline_masking_failure_drops_content_rather_than_leaking_it():
    """Fail CLOSED: an unmaskable chunk contributes nothing to the prompt."""
    db = _db_with("done", [])

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            side_effect=RuntimeError("detector unavailable"),
        ),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert masked[0].content == ""
    assert "555-1234" not in masked[0].content


def test_a_swallowed_detector_failure_withholds_the_chunk():
    """MEASURED FAIL-OPEN: the inline path claimed to fail closed and did not.

    ``detect_segment_spans`` swallows a PII-detector exception and returns the
    spans it collected, so ``_mask_inline``'s ``except`` clause never fired: with
    Presidio broken or absent it masked nothing, returned the chunk **verbatim**,
    and ``mask_chunks`` labelled it ``was_masked=True`` and sent it to the
    provider. The ``failures`` sink is the only thing that separates "found
    nothing" from "could not look".
    """
    db = _db_with("done", [])  # no segments → the inline fallback

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True, categories=("pii",)),
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            side_effect=_detector_that_swallows("pii"),
        ),
        # No spans → the real masker returns the input unchanged. That IS the leak.
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=lambda text, *_a, **_k: (text, []),
        ),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert masked[0].content == ""
    assert "555-1234" not in masked[0].content


def test_the_withheld_chunk_reaches_no_prompt():
    """The property that matters: the raw text is not in what the provider gets.

    Asserting on ``MaskedChunk.content`` alone would still pass if the prompt
    layer read some other attribute, so this drives the real excerpt renderer —
    the last stage before the text leaves the deployment.
    """
    from app.services.chat.prompting import format_excerpts

    db = _db_with("done", [])

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True, categories=("pii",)),
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            side_effect=_detector_that_swallows("pii"),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=lambda text, *_a, **_k: (text, []),
        ),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    block, excerpt_ids = format_excerpts(masked, budget_chars=4000)

    assert "555-1234" not in block
    assert excerpt_ids == [], "a withheld chunk must not be offered as a citation either"


def test_a_detector_failure_outside_the_users_categories_still_answers():
    """The narrowness, which is a requirement and not a nicety.

    A CPU-only deployment has no Presidio at all and has never enabled ``pii``
    — it is not in ``DEFAULT_REDACTION_CATEGORIES``. Blanket fail-closed would
    withhold every excerpt on those deployments over a category the user never
    asked to mask, so only a detector feeding an ENABLED category may withhold.
    """
    db = _db_with("done", [])

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True, categories=("profanity",)),
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            side_effect=_detector_that_swallows("pii"),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            return_value=("profanity-masked text", []),
        ),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert masked[0].content == "profanity-masked text"
    assert masked[0].was_masked is True


def test_a_profanity_failure_withholds_when_custom_words_are_masked():
    """`failures` names DETECTORS, `enabled_categories` names CATEGORIES.

    They coincide for ``pii`` and diverge for ``profanity``, which also produces
    the ``custom`` (user wordlist) category. A user masking only ``custom``
    depends on the profanity detector, so its failure must withhold — a mapping
    that compared the two name-for-name would miss exactly this case.
    """
    db = _db_with("done", [])

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True, categories=("custom",)),
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            side_effect=_detector_that_swallows("profanity"),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=lambda text, *_a, **_k: (text, []),
        ),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert masked[0].content == ""


def _real_cfg(categories, entities=None):
    """The REAL ``EffectiveRedactionConfig``, not the SimpleNamespace stand-in.

    The tests below run the real detector layer and the real masker, so the
    config has to carry every field both of them read.
    """
    from app.core import constants as C  # noqa: N812
    from app.services.redaction.config import EffectiveRedactionConfig

    return EffectiveRedactionConfig(
        enabled=True,
        redact_before_llm=True,
        enabled_categories=set(categories),
        pii_entities=set(C.REDACTION_PII_ENTITIES if entities is None else entities),
    )


def _mask_chunks_with_analyzer(analyzer, cfg):
    """Drive ``mask_chunks`` down the inline path with a given Presidio analyzer.

    Only ``_get_analyzer`` is stubbed. ``detect_segment_spans``, ``detect_pii``,
    the wordlist detector and ``mask_segment`` are all the real thing, because the
    defect being guarded lives *between* those layers: a stub of any one of them
    would model the code we wish existed.
    """
    db = _db_with("done", [])  # no segments → the inline fallback
    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=cfg,
        ),
        patch(
            "app.services.redaction.detectors.pii_presidio._get_analyzer",
            return_value=analyzer,
        ),
    ):
        return mask_chunks(db, [_chunk()], user_id=1)


class _AnalyzerThatThrows:
    """Presidio built fine, then threw — the second row of the measured table."""

    def analyze(self, text, language):  # noqa: ARG002
        raise RuntimeError("nlp engine exploded")


def test_an_absent_presidio_withholds_the_chunk_when_pii_is_enabled():
    """MEASURED FAIL-OPEN, one layer below the sink (issue #403 task #77).

    ``_mask_inline`` can only fail closed on what ``detect_segment_spans``
    records, and that function only recorded a detector that raised *out of*
    ``detect_pii``. ``pii_presidio`` swallowed its own failure one level lower:
    ``_get_analyzer`` caught an absent or unbuildable Presidio, logged "PII
    detection disabled" and returned ``None``, and ``detect_pii`` then returned
    ``[]`` — indistinguishable from a clean segment.

    Measured on HEAD before the fix, real detector layer, only the analyzer
    removed (which IS the deployment being modelled — a box with no working
    Presidio, the common CPU-only case):

        analyzer is None          -> spans=[] failures=[]      blocking={}      PASSES THROUGH
        analyzer.analyze() raises -> spans=[] failures=[]      blocking={}      PASSES THROUGH
        detect_pii() raises       -> spans=[] failures=['pii'] blocking={'pii'} withheld

    Only the third reached the sink and it is the least likely of the three, so a
    user who had explicitly ENABLED ``pii`` still got the chunk sent unmasked
    while ``mask_chunks`` reported ``was_masked=True``.
    """
    masked = _mask_chunks_with_analyzer(None, _real_cfg({"pii"}))

    assert masked[0].content == ""
    assert "555-1234" not in masked[0].content


def test_a_presidio_that_throws_withholds_the_chunk_when_pii_is_enabled():
    """Row 2. A built analyzer that raises used to be skipped chunk-by-chunk.

    Twin of the test above and not a duplicate of it: the two faults were swallowed
    in different places (``_get_analyzer`` returning ``None`` vs an ``except:
    continue`` inside the chunk loop) and are given different dispositions by
    ``detect_and_store``. Both must withhold here.
    """
    masked = _mask_chunks_with_analyzer(_AnalyzerThatThrows(), _real_cfg({"pii"}))

    assert masked[0].content == ""


def test_an_absent_presidio_still_answers_when_pii_was_never_enabled():
    """THE regression that would hurt most users — the CPU-only default deployment.

    ``pii`` is deliberately NOT in ``DEFAULT_REDACTION_CATEGORIES``, so a box with
    no working Presidio and a user who never asked for PII masking must lose
    nothing: blanket fail-closed would empty every excerpt over a category nobody
    enabled. That narrowness is what makes the gate above safe, and it is the
    property most likely to be broken by "just fail closed on any failure".

    Runs the real wordlist detector and the real masker over profanity-free text,
    so the assertion is that the content SURVIVES, not merely that it is non-empty.
    """
    masked = _mask_chunks_with_analyzer(None, _real_cfg({"profanity", "toxicity", "custom"}))

    assert masked[0].content == "my number is 555-1234"
    assert masked[0].was_masked is True


def test_the_cached_path_refuses_a_scan_that_never_ran_pii():
    """The primary path discriminates on detector COVERAGE, not just status (#78).

    ``_mask_inline`` is the FALLBACK. The path most requests take is
    ``_mask_from_segments``, which trusts cached spans whenever
    ``redaction_status == done`` — and an unavailable detector is recorded as a
    *skip*, so a scan that never ran Presidio still reaches ``done``. The segments
    then say "no PII spans", the masker masks nothing, and a ``pii``-enabled user
    gets the raw text with ``was_masked=True``. Same leak as the one just closed,
    through the primary path instead of the fallback.

    Marking those scans FAILED instead is not the fix: ``llm_guard`` turns FAILED
    into a non-retryable refusal, which would permanently break summarization,
    speaker-ID and topic extraction on any deployment with no Presidio. And the
    masker cannot probe for itself — the API process and the ``celery-redaction``
    worker load different models, so "can I load Presidio here" answers a
    different question from "did the detector that produced these spans have it".
    Closing it needed durable per-file **detector coverage**, which arrived as
    ``media_file.redaction_coverage`` (v391). ``uncovered_detectors`` now sits
    beside the status check, and a gap returns None so the chunk falls through to
    ``_mask_inline`` — which runs the detector here and now, and fails closed.

    The subject is the **requesting user's** policy, not the file owner's: one
    turn retrieves across a library of shared recordings with no single owner.
    That is the opposite of ``llm_guard``, deliberately.
    """
    # A REAL phone number, not the old 555-1234. Presidio does not recognise a
    # 7-digit fragment, so the previous version of this guard passed whether the
    # cached path or the inline path ran — it asserted the hazard with a string
    # that could not demonstrate it either way.
    leaky = "my number is 555-867-5309"
    segment = SimpleNamespace(
        text=leaky,
        redactions=None,  # the scan cached nothing for PII because it never looked
        words=None,
    )
    # DONE, but the scan ran without `pii` — the exact state v391 records.
    db = _db_with("done", [segment], coverage=["profanity", "toxicity"])

    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_real_cfg({"pii"}),
    ):
        masked = mask_chunks(db, [_chunk(leaky)], user_id=1)

    assert "555-867-5309" not in masked[0].content, (
        "the cached path trusted a scan whose PII detector never ran: the raw "
        "number reached the prompt with was_masked=True"
    )
    assert masked[0].was_masked is True


def test_unresolvable_policy_fails_closed():
    """If we can't tell whether masking is required, we must not send the text."""
    db = MagicMock()
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        side_effect=RuntimeError("db down"),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert masked[0].content == ""
    assert masked[0].was_masked is True


def test_masked_chunk_keeps_citation_metadata():
    """Masking changes text only — timestamps and speaker still drive citations."""
    db = MagicMock()
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=True, redact_before_llm=False),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)[0]

    assert masked.file_uuid == "11111111-1111-1111-1111-111111111111"
    assert masked.speaker == "Dana"
    assert masked.start_time == 10.0
    assert masked.title == "Call"
