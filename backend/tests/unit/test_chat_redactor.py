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


def _cfg(*, enabled: bool, redact_before_llm: bool):
    return SimpleNamespace(enabled=enabled, redact_before_llm=redact_before_llm)


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


def test_policy_on_rebuilds_text_from_cached_segment_spans():
    """Primary path: cached spans make masking sub-millisecond, no detectors run."""
    db = MagicMock()
    segment = SimpleNamespace(text="my number is 555-1234", redactions=[], words=None)
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [segment]

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch("app.utils.transcript_builders._seg_text", return_value="my number is [PHONE]"),
    ):
        masked = mask_chunks(db, [_chunk()], user_id=1)

    assert masked[0].content == "my number is [PHONE]"
    assert masked[0].was_masked is True
    assert "555-1234" not in masked[0].content


def test_falls_back_to_inline_masking_when_segments_are_missing():
    """Files whose detection hasn't finished still get masked, just more slowly."""
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

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
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

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
