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


def _db_with(status, segments):
    """A db mock that answers the status probe and the segment query distinctly."""
    db = MagicMock()
    status_q = MagicMock()
    status_q.filter.return_value.scalar.return_value = status
    seg_q = MagicMock()
    seg_q.filter.return_value.order_by.return_value.all.return_value = segments
    db.query.side_effect = [status_q, seg_q]
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


def test_an_absent_presidio_never_reaches_the_failures_sink():
    """MUST-FIRE. The residual hole one layer BELOW the fix above (measured).

    ``_mask_inline`` can only fail closed on what ``detect_segment_spans``
    records, and that function only records a detector that raises *out of*
    ``detect_pii``. ``pii_presidio`` swallows its own failures one level lower:
    ``_get_analyzer`` catches an absent or unbuildable Presidio, logs "PII
    detection disabled", latches ``_load_failed`` and returns ``None``, and
    ``detect_pii`` then returns ``[]`` — indistinguishable from a clean segment.

    Measured, running the REAL detector layer and the REAL masker with only the
    analyzer taken away (which IS the deployment being modelled):

        analyzer is None      -> spans=[] failures=[]      blocking={} -> PASSES THROUGH
        analyzer.analyze()    -> spans=[] failures=[]      blocking={} -> PASSES THROUGH
          raises
        detect_pii() raises   -> spans=[] failures=['pii'] blocking={'pii'} -> withheld

    So a user who HAS enabled ``pii`` on a deployment with no Presidio still gets
    the chunk sent unmasked while ``mask_chunks`` labels it masked. That is the
    same swallowed-failure shape this module just closed, and the sink is the
    only thing that could separate the two — it just is not fed here.

    **This test asserts the hazard, not the desired behaviour.** When the lower
    layer learns to report unavailability, this goes RED: delete it and assert
    the chunk is withheld instead. Do not "fix" it by relaxing the assertion.
    """
    from app.core import constants as C  # noqa: N812
    from app.services.redaction.config import EffectiveRedactionConfig

    real_cfg = EffectiveRedactionConfig(
        enabled=True,
        redact_before_llm=True,
        enabled_categories={"pii"},
        pii_entities=set(C.REDACTION_PII_ENTITIES),
    )
    db = _db_with("done", [])  # no segments → the inline fallback

    sink: list[str] = []
    real_detect = __import__(
        "app.services.redaction.service", fromlist=["RedactionService"]
    ).RedactionService.detect_segment_spans

    def _spy(text, words, det_cfg, **kwargs):
        # Hand the REAL function our own sink so the test can read what it
        # recorded, then report it onward exactly as it was given.
        kwargs["failures"] = sink
        return real_detect(text, words, det_cfg, **kwargs)

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=real_cfg,
        ),
        # Presidio absent / analyzer unbuildable — nothing else is stubbed.
        patch(
            "app.services.redaction.detectors.pii_presidio._get_analyzer",
            return_value=None,
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            side_effect=_spy,
        ),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert sink == [], (
        "the sink now sees an absent PII detector — the lower layer was fixed, "
        "so delete this guard and assert the chunk is WITHHELD instead"
    )
    assert masked[0].content == "my number is 555-1234", (
        "an absent Presidio no longer passes the chunk through; re-derive this guard"
    )


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
