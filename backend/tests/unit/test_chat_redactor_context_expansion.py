"""Issue #523: context expansion wired into ``redactor.mask_chunks``.

The reproduction case from the issue: a speaker-scoped question over a
multi-party meeting retrieves a near-empty fragment ("Y") while the
substantive material sits in the immediately surrounding exchange, on both
sides. ``test_flag_on_expands_the_fragment_to_its_surrounding_exchange`` is
the RED-then-GREEN test — ``mask_chunks`` had no ``expand_short_chunks``
parameter at all before this issue, so it fails against the pre-fix source
(``TypeError: unexpected keyword argument``) and passes after.

Three more properties this module is not allowed to get wrong:

* the flag OFF must be byte-identical to today (``test_flag_off_...``);
* expanded text takes the SAME masking as any other excerpt, including
  failing closed — a PII span in the neighbouring material must be masked,
  not just the original short fragment's own text;
* expansion is bounded, so it cannot silently crowd another file's evidence
  out of ``prompting.format_excerpts``'s hard budget ceiling.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.chat.prompting import format_excerpts
from app.services.chat.redactor import MaskedChunk
from app.services.chat.redactor import mask_chunks
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


@contextmanager
def _one_session(db):
    yield db


def _factory(db):
    """Same session-factory shape ``test_chat_redactor.py`` uses — every call
    to it returns the SAME underlying db double, matching how the real
    ``expand_short_chunks`` gather and the masking gather that follows it
    are two separate ``with session_factory() as db:`` blocks in production."""
    return lambda: _one_session(db)


def _chunk(content: str = "Y", *, start: float = 100.0, end: float = 101.0) -> ChunkHit:
    return ChunkHit(
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=5,
        chunk_index=3,
        content=content,
        title="TS3005 planning",
        speaker="Marketing",
        start_time=start,
        end_time=end,
    )


def _seg(text: str, start: float, end: float, redactions=None) -> SimpleNamespace:
    return SimpleNamespace(
        text=text, start_time=start, end_time=end, redactions=redactions or [], words=None
    )


def _cfg(*, enabled: bool, redact_before_llm: bool, categories=("pii", "profanity", "custom")):
    return SimpleNamespace(
        enabled=enabled, redact_before_llm=redact_before_llm, enabled_categories=set(categories)
    )


def _scan_row(status: str = "done", coverage=None, language: str = "en", user_id: int = 1):
    return SimpleNamespace(
        id=1,
        redaction_status=status,
        redaction_coverage=coverage,
        language=language,
        user_id=user_id,
    )


class _ListQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class _ScanQuery:
    def __init__(self, scan):
        self._scan = scan

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._scan


class _ExpandThenMaskDB:
    """Answers the query sequence ``mask_chunks(expand_short_chunks=True)``
    issues for ONE chunk, in order: the expansion-window segment read, the
    redaction scan probe, then the masking rebuild's segment read.

    When ``expand_short_chunks`` is False, only the last two queries happen —
    tests that exercise that path construct this with ``expansion_segments``
    unused and pass ``expect_expansion_query=False``.
    """

    def __init__(self, expansion_segments, scan, rebuild_segments, *, expect_expansion_query=True):
        self._expansion_segments = expansion_segments
        self._scan = scan
        self._rebuild_segments = rebuild_segments
        self._expect_expansion_query = expect_expansion_query
        self._calls = 0

    def query(self, *entities, **kwargs):
        self._calls += 1
        offset = 1 if self._expect_expansion_query else 0
        if self._expect_expansion_query and self._calls == 1:
            return _ListQuery(self._expansion_segments)
        if self._calls == 1 + offset:
            return _ScanQuery(self._scan)
        return _ListQuery(self._rebuild_segments)


def _mask_segment_passthrough(text, spans, words, cfg, allowlist):
    """Stand-in for ``RedactionService.mask_segment`` with no PII spans."""
    return text, []


def test_flag_off_leaves_a_content_free_fragment_content_free():
    """Control: today's behaviour, byte-identical. This is the failure #523
    describes — a real, substantive exchange sits around 'Y' and none of it
    reaches the prompt when expansion is off (the default)."""
    db = _ExpandThenMaskDB(
        expansion_segments=[],
        scan=_scan_row(),
        rebuild_segments=[_seg("Y", 100.0, 101.0)],
        expect_expansion_query=False,
    )

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=_mask_segment_passthrough,
        ),
    ):
        masked = mask_chunks(_factory(db), [_chunk("Y")], user_id=1)

    assert masked[0].content.strip() == "Y"


def test_flag_off_never_queries_for_expansion_segments():
    """The flag being unset must not merely produce the same output — it
    must not do the extra DB work at all."""
    db = _ExpandThenMaskDB(
        expansion_segments=[],
        scan=_scan_row(),
        rebuild_segments=[_seg("Y", 100.0, 101.0)],
        expect_expansion_query=False,
    )

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=_mask_segment_passthrough,
        ),
    ):
        mask_chunks(_factory(db), [_chunk("Y")], user_id=1, expand_short_chunks=False)

    # Exactly the scan + rebuild queries — no third (expansion) query.
    assert db._calls == 2


def test_flag_on_expands_the_fragment_to_its_surrounding_exchange():
    """RED before this issue (``expand_short_chunks`` did not exist), GREEN
    after: the substantive material on both sides of the fragment is what
    the model actually sees."""
    expansion_segments = [
        _seg("What did the Marketing role contribute to the launch?", 90.0, 96.0),
        _seg("Y", 100.0, 101.0),
        _seg("We redesigned the pricing page and ran the launch campaign.", 102.0, 109.0),
    ]
    rebuild_segments = [
        _seg("What did the Marketing role contribute to the launch?", 90.0, 96.0),
        _seg("Y", 100.0, 101.0),
        _seg("We redesigned the pricing page and ran the launch campaign.", 102.0, 109.0),
    ]
    db = _ExpandThenMaskDB(expansion_segments, _scan_row(), rebuild_segments)

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=_mask_segment_passthrough,
        ),
    ):
        masked = mask_chunks(_factory(db), [_chunk("Y")], user_id=1, expand_short_chunks=True)

    assert "launch campaign" in masked[0].content
    assert "contribute to the launch" in masked[0].content
    assert masked[0].content.strip() != "Y"


def test_expanded_pii_is_masked_not_just_the_original_fragment():
    """Security assertion, not a nicety: a PII span living in the
    NEIGHBOURING material — not in the original short chunk itself — must
    still be masked once expansion pulls it into the prompt."""
    expansion_segments = [
        _seg("Call me back at 555-1234 after the sync.", 90.0, 96.0),
        _seg("Y", 100.0, 101.0),
    ]
    rebuild_segments = [
        _seg(
            "Call me back at 555-1234 after the sync.",
            90.0,
            96.0,
            redactions=[
                {"char_start": 16, "char_end": 24, "category": "pii", "entity_type": "PHONE"}
            ],
        ),
        _seg("Y", 100.0, 101.0, redactions=[]),
    ]
    db = _ExpandThenMaskDB(expansion_segments, _scan_row(), rebuild_segments)

    def _mask_with_phone_redaction(text, spans, words, cfg, allowlist):
        if spans:
            return "Call me back at [PHONE] after the sync.", spans
        return text, []

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=_mask_with_phone_redaction,
        ),
    ):
        masked = mask_chunks(_factory(db), [_chunk("Y")], user_id=1, expand_short_chunks=True)

    assert "555-1234" not in masked[0].content
    assert "[PHONE]" in masked[0].content


def test_expansion_still_fails_closed_when_masking_errors():
    """Expansion must not create a bypass around the fail-closed contract —
    a masking error on the (now larger) content still withholds it."""
    expansion_segments = [_seg("secret info", 95.0, 99.0), _seg("Y", 100.0, 101.0)]
    rebuild_segments = [_seg("secret info", 95.0, 99.0), _seg("Y", 100.0, 101.0)]
    db = _ExpandThenMaskDB(expansion_segments, _scan_row(), rebuild_segments)

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            side_effect=RuntimeError("presidio blew up"),
        ),
    ):
        masked = mask_chunks(_factory(db), [_chunk("Y")], user_id=1, expand_short_chunks=True)

    assert masked[0].content == ""
    assert masked[0].was_masked is True


def test_expanded_content_still_respects_the_excerpt_budget():
    """Expansion competes for the SAME hard budget as any other excerpt.
    This is a regression check that widening a chunk does not bypass
    ``format_excerpts``'s ceiling — the mechanism that stops one file's
    evidence from silently evicting another's (#517's failure mode)."""
    wide_source = _chunk("expanded " * 100, start=10.0, end=90.0)
    wide = MaskedChunk(source=wide_source, content="expanded " * 100)

    other_source = _chunk("short different file", start=500.0, end=505.0)
    other_source.file_uuid = "22222222-2222-2222-2222-222222222222"
    other = MaskedChunk(source=other_source, content="short different file evidence")

    block, _ids = format_excerpts([wide, other], budget_chars=400)

    assert len(block) <= 400
