"""Egress masking must never reveal the original text (confirmed leak, task egress-style).

``EffectiveRedactionConfig.style`` is a DISPLAY choice — the UI blurs ``blur``
markup via CSS and reveals it on hover for an authorized viewer, and
``first_letter`` deliberately shows one character so a reader can recognize
what was hidden without full disclosure. Both are correct for a display
surface and both are a LEAK when the "surface" is a prompt sent to a remote
LLM provider: ``redaction/spans.py::_placeholder`` embeds the ORIGINAL,
UNMASKED text inside the ``blur`` markup, and ``chat/redactor.py`` used to
pass ``cfg.style`` straight through to ``RedactionService.mask_segment`` on
every egress path — so a user who set their redaction style to ``blur``
(a user-settable preference, see ``api/endpoints/redaction_settings.py``) had
100% of "masked" chunk/digest text sent to a third-party provider verbatim.

These tests assert on the OUTGOING TEXT the egress functions produce — never
on whether a masker was *called* — and the headline test drives the real
``LLMService`` against the real mock LLM server's ``mock-echo`` scenario,
which echoes back exactly what it received, so the assertion is against real
outgoing bytes.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core import constants as C  # noqa: N812
from app.services.chat import prompting
from app.services.chat.redactor import _mask_from_spans
from app.services.chat.redactor import _SegmentSpans
from app.services.chat.redactor import mask_chunks
from app.services.chat.redactor import mask_digests
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.spans import apply_redactions
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit

SECRET = "Ada Lovelace"
CONTENT = f"The witness, {SECRET}, signed the affidavit at noon."
_START = CONTENT.index(SECRET)
_END = _START + len(SECRET)
_SPAN = {"char_start": _START, "char_end": _END, "category": "pii", "entity_type": "PERSON"}


def _cfg(style: str) -> EffectiveRedactionConfig:
    return EffectiveRedactionConfig(
        enabled=True,
        redact_before_llm=True,
        enabled_categories={"pii"},
        pii_entities={"PERSON"},
        style=style,
    )


# --------------------------------------------------------------------------- #
# _egress_style itself
#
# Imported INSIDE each test, deliberately, rather than at module scope: this
# helper is new in this fix and does not exist on pre-fix HEAD. A module-level
# import would fail the whole file's COLLECTION under a red-check (git-archive
# tree at the pre-fix commit), which reports as an ImportError rather than the
# behavioural assertion failure that is the actual point of a red-check. Every
# other test below imports only names that exist on both sides of the fix, so
# they run — and fail on their assertions — against the old code.
# --------------------------------------------------------------------------- #


def test_egress_style_leaves_an_already_label_config_untouched():
    from app.services.chat.redactor import _egress_style

    cfg = _cfg("label")
    assert _egress_style(cfg) is cfg


@pytest.mark.parametrize("style", ["blur", "first_letter", "asterisks"])
def test_egress_style_forces_every_revealing_style_to_label(style):
    from app.services.chat.redactor import _egress_style

    forced = _egress_style(_cfg(style))
    assert forced.style == "label"


def test_egress_style_tolerates_a_duck_typed_config_without_dataclasses_replace():
    """A test double (e.g. ``SimpleNamespace``) must not crash ``_egress_style``.

    ``dataclasses.replace`` raises on a non-dataclass instance; the fallback
    branch copies and overrides instead, so a caller passing a duck-typed cfg
    (as several tests in this package's sibling files do) is not itself proof
    the forcing "worked" here — just that it degrades safely.
    """
    from app.services.chat.redactor import _egress_style

    double = SimpleNamespace(style="blur", enabled=True)
    forced = _egress_style(double)
    assert forced.style == "label"
    assert double.style == "blur", "the caller's own object must not be mutated"


# --------------------------------------------------------------------------- #
# The display path is UNCHANGED — same cfg, called directly against spans.py,
# exactly as a transcript/export display caller (never chat/redactor.py) does.
# --------------------------------------------------------------------------- #


def test_blur_still_blurs_on_the_real_display_path():
    display_cfg = _cfg("blur")
    masked, _applied = apply_redactions(
        CONTENT, [_SPAN], style=display_cfg.style, enabled_categories=display_cfg.enabled_categories
    )
    assert SECRET in masked, "blur must still embed the original for an authorized display reader"
    assert 'class="redacted"' in masked


def test_first_letter_still_leaks_the_first_character_on_the_display_path():
    display_cfg = _cfg("first_letter")
    masked, _applied = apply_redactions(
        CONTENT, [_SPAN], style=display_cfg.style, enabled_categories=display_cfg.enabled_categories
    )
    assert "A" + "*" * (len(SECRET) - 1) in masked


# --------------------------------------------------------------------------- #
# _mask_from_spans — the pure Phase-B function both mask_chunks (cached path)
# and mask_digests (cached path) call to build provider-bound text.
# --------------------------------------------------------------------------- #


def test_mask_from_spans_blur_does_not_leak_the_secret():
    segments = [_SegmentSpans(text=CONTENT, redactions=[_SPAN], words=None)]
    masked = _mask_from_spans(segments, _cfg("blur"))
    assert SECRET not in masked
    assert "<span" not in masked, "no blur markup — and therefore no embedded original — may egress"
    assert "[PERSON]" in masked


def test_mask_from_spans_first_letter_does_not_leak_even_one_character():
    segments = [_SegmentSpans(text=CONTENT, redactions=[_SPAN], words=None)]
    masked = _mask_from_spans(segments, _cfg("first_letter"))
    assert SECRET not in masked
    assert "A" + "*" * (len(SECRET) - 1) not in masked
    assert "[PERSON]" in masked


def test_mask_from_spans_asterisks_is_forced_to_label_too():
    segments = [_SegmentSpans(text=CONTENT, redactions=[_SPAN], words=None)]
    masked = _mask_from_spans(segments, _cfg("asterisks"))
    assert SECRET not in masked
    assert "*" * len(SECRET) not in masked
    assert "[PERSON]" in masked


def test_mask_from_spans_label_is_unaffected():
    segments = [_SegmentSpans(text=CONTENT, redactions=[_SPAN], words=None)]
    masked = _mask_from_spans(segments, _cfg("label"))
    assert SECRET not in masked
    assert "[PERSON]" in masked


# --------------------------------------------------------------------------- #
# mask_chunks / mask_digests, end to end — mocked DB, REAL EffectiveRedactionConfig,
# REAL RedactionService.mask_segment (not mocked): the actual egress boundary.
# --------------------------------------------------------------------------- #


@contextmanager
def _one_session(db):
    yield db


def _factory(db):
    return lambda: _one_session(db)


def _chunk_db(segment, *, user_id=1):
    db = MagicMock()
    scan_q = MagicMock()
    scan_q.filter.return_value.first.return_value = SimpleNamespace(
        id=1,
        redaction_status=C.REDACTION_STATUS_DONE,
        redaction_coverage=None,
        language="en",
        user_id=user_id,
    )
    seg_q = MagicMock()
    seg_q.filter.return_value.order_by.return_value.all.return_value = [segment]
    db.query.side_effect = [scan_q, seg_q]
    return db


def _chunk_hit(content: str = CONTENT) -> ChunkHit:
    return ChunkHit(
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=5,
        chunk_index=0,
        content=content,
        title="Deposition",
        speaker="Dana",
        start_time=10.0,
        end_time=40.0,
    )


def test_mask_chunks_end_to_end_never_egresses_the_secret_under_blur():
    segment = SimpleNamespace(text=CONTENT, redactions=[_SPAN], words=None)
    db = _chunk_db(segment)

    with patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg("blur")):
        masked = mask_chunks(_factory(db), [_chunk_hit()], user_id=1)

    assert SECRET not in masked[0].content
    assert "<span" not in masked[0].content
    assert masked[0].was_masked is True


def _digest_hit(content: str = CONTENT) -> ChunkHit:
    return ChunkHit(
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=5,
        chunk_index=-1,
        content=content,
        title="Deposition summary",
        speaker=None,
        start_time=12.5,
        end_time=3400.0,
        digest_section=0,
    )


def _digest_db(sentence_segment_rows, *, user_id=1):
    digest_payload = {
        "sections": [
            {
                "index": 0,
                "sentences": [
                    {
                        "text": CONTENT,
                        "order": 0,
                        "speaker": "Dana",
                        "provenance": {
                            "kind": "segment_ids",
                            "segment_ids": [1],
                            "start_time": 12.5,
                            "end_time": 20.0,
                        },
                    }
                ],
            }
        ]
    }

    def _query(*targets):
        key = " ".join(str(t) for t in targets)
        result = MagicMock()
        if "redaction_status" in key:
            result.filter.return_value.first.return_value = SimpleNamespace(
                id=1,
                redaction_status=C.REDACTION_STATUS_DONE,
                redaction_coverage=None,
                language="en",
                user_id=user_id,
            )
        elif "digest" in key:
            result.filter.return_value.all.return_value = [(5, digest_payload)]
        else:
            result.filter.return_value.order_by.return_value.all.return_value = (
                sentence_segment_rows
            )
        return result

    db = MagicMock()
    db.query.side_effect = _query
    return db


def test_mask_digests_end_to_end_never_egresses_the_secret_under_blur():
    segment_row = SimpleNamespace(id=1, text=CONTENT, redactions=[_SPAN], words=None)
    db = _digest_db([segment_row])

    with patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg("blur")):
        masked = mask_digests(_factory(db), [_digest_hit()], user_id=1)

    assert SECRET not in masked[0].content
    assert "<span" not in masked[0].content
    assert masked[0].was_masked is True


# --------------------------------------------------------------------------- #
# The headline assertion: real outgoing bytes, via the real mock LLM server's
# `mock-echo` scenario, which echoes back exactly what it received.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _allow_the_loopback_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mock LLM is always on loopback; the SSRF guard refuses that by default.

    Without this, every request below is BLOCKED and would come back empty —
    which would make the "secret absent" assertion pass for the wrong reason
    (nothing was sent at all). Set on the test, not exported repo-wide, exactly
    as ``test_llm_reasoning_not_rendered_as_answer.py`` does.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", True)


def test_the_secret_never_reaches_the_provider_over_the_wire(mock_llm_url):
    """Full path: mask_chunks -> format_excerpts -> real LLMService -> real HTTP.

    ``mock-echo`` returns exactly what it received, so asserting on its
    response IS asserting on the bytes the app actually sent — not on whether
    a masking function was invoked.
    """
    from app.services.llm_service import LLMConfig
    from app.services.llm_service import LLMProvider
    from app.services.llm_service import LLMService

    segment = SimpleNamespace(text=CONTENT, redactions=[_SPAN], words=None)
    db = _chunk_db(segment)
    with patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg("blur")):
        masked = mask_chunks(_factory(db), [_chunk_hit()], user_id=1)

    excerpt_block, used_ids = prompting.format_excerpts(masked, budget_chars=4000)
    assert used_ids, "the excerpt must have made it into the prompt at all"
    assert SECRET not in excerpt_block, "sanity: the prompt built locally must not carry the secret"

    service = LLMService(
        LLMConfig(
            provider=LLMProvider.VLLM,
            model="mock-echo",
            base_url=mock_llm_url,
            api_key="mock-key-not-secret",
        )
    )
    response = service.chat_completion(
        [
            {"role": "system", "content": "Answer only from the excerpts below."},
            {"role": "user", "content": excerpt_block},
        ]
    )

    assert SECRET not in response.content, (
        "the secret reached the provider verbatim — this is the confirmed egress leak"
    )
    assert "[PERSON]" in response.content, "the label-style placeholder must have been echoed back"
