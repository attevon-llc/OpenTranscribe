"""Chat egress masking: fail-closed branches that no other test could see break.

Each test here exists because a mutation of ``chat/redactor*.py`` survived the whole
masking suite when the redactor was split by concern: the mutated code sent raw or
under-masked text towards the provider and every existing test stayed green.

* The file OWNER's per-org redaction floor (#988): dropping ``organization_id`` from
  the owner-side ``resolve_effective_config`` call.
* ``mask_digests``' per-SENTENCE fail-closed unit: a sentence whose masking raised
  contributing raw text, and a sentence whose segment read raised taking the whole
  section down to the inline masker.
* A digest sentence carrying a provenance kind the transcript masker does not
  understand: read by guessing instead of withheld.
* A digest whose scan probe raised: masked under the requester's policy alone instead
  of the most restrictive one.
* A digest whose scan did not cover a detector the policy relies on: served from the
  cached spans instead of falling through to the inline masker.
* A chunk whose gather raised: passed through, or returned raw, instead of withheld.

Masking itself is the REAL ``RedactionService.mask_segment``; only the session, the
policy resolve and (where named) the detector are stand-ins.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core import redaction_defaults
from app.services.chat.redactor import mask_chunks
from app.services.chat.redactor import mask_digests
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit

PHONE = "555-867-5309"
CONTENT = f"Call Dana at {PHONE} tomorrow."
_PHONE_SPAN = {
    "char_start": CONTENT.index(PHONE),
    "char_end": CONTENT.index(PHONE) + len(PHONE),
    "category": "pii",
    "entity_type": "PHONE",
}
CLEAN = "We agreed the budget."
LEAKY = "Dana's card is 4111 1111 1111 1111."
REQUESTER_ID = 1
OWNER_ID = 999
OWNER_ORG_ID = 42
_RESOLVE = "app.services.redaction.config.resolve_effective_config"
_DETECT = "app.services.redaction.service.RedactionService.detect_segment_spans"


@contextmanager
def _one_session(db):
    yield db


def _factory(db):
    return lambda: _one_session(db)


def _permissive() -> EffectiveRedactionConfig:
    return EffectiveRedactionConfig(enabled=False, redact_before_llm=False)


def _strict() -> EffectiveRedactionConfig:
    return EffectiveRedactionConfig(
        enabled=True,
        redact_before_llm=True,
        enabled_categories={"pii"},
        pii_entities={"PHONE"},
    )


def _resolve_with_owner_org_floor(_db, user_id, organization_id=None):
    """Only the owner, resolved INSIDE their own org, carries the masking floor."""
    if user_id == OWNER_ID and organization_id == OWNER_ORG_ID:
        return _strict()
    return _permissive()


def _resolve_strict_requester(_db, user_id, organization_id=None):
    return _strict() if user_id == REQUESTER_ID else _permissive()


def _detect_the_phone(text_in, _words, _det_cfg, *, failures=None, **_kwargs):
    start = text_in.find(PHONE)
    spans = (
        [] if start < 0 else [{**_PHONE_SPAN, "char_start": start, "char_end": start + len(PHONE)}]
    )
    return spans, None


def _scan_row(organization_id=None, coverage=None):
    return SimpleNamespace(
        id=5,
        redaction_status=redaction_defaults.REDACTION_STATUS_DONE,
        redaction_coverage=coverage,
        language="en",
        user_id=OWNER_ID,
        organization_id=organization_id,
    )


def _segment(text: str, redactions: list[dict]):
    return SimpleNamespace(text=text, redactions=redactions, words=None)


def _chunk_db(organization_id):
    db = MagicMock()
    scan_q = MagicMock()
    scan_q.filter.return_value.first.return_value = _scan_row(organization_id)
    seg_q = MagicMock()
    seg_q.filter.return_value.order_by.return_value.all.return_value = [
        _segment(CONTENT, [_PHONE_SPAN])
    ]
    db.query.side_effect = [scan_q, seg_q]
    return db


def _digest_db(sentence_reads, *, scan=None, kinds=None):
    """Answers the batched digest read, the scan probe, then one segment read per sentence.

    ``sentence_reads`` is consumed IN ORDER, one entry per segment read actually issued
    (a MagicMock cannot evaluate an ``IN`` clause); an exception entry makes that read
    raise. ``scan`` is the scan row, or an exception for the probe to raise.
    """
    kinds = kinds or ["segment_ids"] * len(sentence_reads)
    sentences = [
        {
            "text": f"sentence {n}",
            "order": n,
            "speaker": None,
            "provenance": {"kind": kind, "segment_ids": [n + 1]},
        }
        for n, kind in enumerate(kinds)
    ]
    digest = {"sections": [{"index": 0, "sentences": sentences}]}
    reads = list(sentence_reads)
    scan = _scan_row() if scan is None else scan

    def _query(*targets):
        key = " ".join(str(t) for t in targets)
        result = MagicMock()
        if "redaction_status" in key:
            if isinstance(scan, Exception):
                result.filter.side_effect = scan
            else:
                result.filter.return_value.first.return_value = scan
        elif "digest" in key:
            result.filter.return_value.all.return_value = [(5, digest)]
        else:
            read = reads.pop(0)
            if isinstance(read, Exception):
                result.filter.side_effect = read
            else:
                result.filter.return_value.order_by.return_value.all.return_value = read
        return result

    db = MagicMock()
    db.query.side_effect = _query
    return db


def _chunk() -> ChunkHit:
    return ChunkHit(
        file_uuid="22222222-2222-2222-2222-222222222222",
        file_id=5,
        chunk_index=0,
        content=CONTENT,
        title="Call",
        start_time=0.0,
        end_time=5.0,
    )


def _digest_hit(content: str) -> ChunkHit:
    return ChunkHit(
        file_uuid="22222222-2222-2222-2222-222222222222",
        file_id=5,
        chunk_index=-1,
        content=content,
        title="Call",
        start_time=0.0,
        end_time=5.0,
        digest_section=0,
    )


def _mask_digest(db, content, *, resolve=_resolve_strict_requester, detect=_detect_the_phone):
    with patch(_RESOLVE, side_effect=resolve), patch(_DETECT, side_effect=detect) as detector:
        masked = mask_digests(_factory(db), [_digest_hit(content)], user_id=REQUESTER_ID)
    return masked[0], detector


# ------------------------------------------------------- the owner's org floor (#988)


def test_the_owners_org_floor_masks_a_chunk_the_requester_would_send_raw():
    with patch(_RESOLVE, side_effect=_resolve_with_owner_org_floor):
        masked = mask_chunks(_factory(_chunk_db(OWNER_ORG_ID)), [_chunk()], user_id=REQUESTER_ID)

    assert PHONE not in masked[0].content
    assert masked[0].content.startswith("Call Dana at ")
    assert masked[0].was_masked is True


def test_without_an_owner_org_the_same_chunk_passes_through():
    """Control: the org id, not anything else in the fixture, is what turns masking on."""
    with patch(_RESOLVE, side_effect=_resolve_with_owner_org_floor):
        masked = mask_chunks(_factory(_chunk_db(None)), [_chunk()], user_id=REQUESTER_ID)

    assert masked[0].content == CONTENT
    assert masked[0].was_masked is False


def test_the_owners_org_floor_masks_a_digest_section_too():
    db = _digest_db([[_segment(CONTENT, [_PHONE_SPAN])]], scan=_scan_row(OWNER_ORG_ID))
    masked, _detector = _mask_digest(db, CONTENT, resolve=_resolve_with_owner_org_floor)

    assert PHONE not in masked.content
    assert masked.content.startswith("Call Dana at ")
    assert masked.was_masked is True


# ------------------------------------------------------- per-sentence fail-closed


def test_a_digest_sentence_whose_masking_raises_contributes_nothing():
    """A digest fails closed per SENTENCE: the one that could not be masked is
    withheld whole, and the sentence that masked cleanly still reaches the prompt."""
    db = _digest_db([[_segment(CLEAN, [])], [_segment(LEAKY, [])]])

    def _mask_segment(text, *_args, **_kwargs):
        if text == LEAKY:
            raise RuntimeError("masking blew up on this sentence")
        return text, []

    with patch(
        "app.services.redaction.service.RedactionService.mask_segment",
        side_effect=_mask_segment,
    ):
        masked, _detector = _mask_digest(db, f"{CLEAN} {LEAKY}")

    assert masked.content == CLEAN
    assert masked.was_masked is True


def test_a_digest_sentence_whose_segment_read_raises_is_withheld_alone():
    """The failed read withholds THAT sentence; the section is not handed to the
    inline masker, which would re-detect over the rendered section, raw sentence included."""
    db = _digest_db([[_segment(CLEAN, [])], RuntimeError("segment read failed")])
    masked, detector = _mask_digest(db, f"{CLEAN} {LEAKY}", detect=lambda *_a, **_k: ([], None))

    assert masked.content == CLEAN
    assert detector.call_count == 0


def test_a_foreign_provenance_kind_is_withheld_even_when_it_names_segment_ids():
    """A ``char_range`` (document, #362) sentence reaching the transcript masker is a
    bug; reading its ids as if they were segment ids would be a guess that sends text."""
    db = _digest_db(
        [[_segment(CLEAN, [])], [_segment(LEAKY, [])]],
        kinds=["segment_ids", "char_range"],
    )
    masked, _detector = _mask_digest(db, f"{CLEAN} {LEAKY}")

    assert masked.content == CLEAN


def test_a_digest_scan_probe_failure_masks_under_the_most_restrictive_policy():
    """The probe raising leaves the owner unknown: the section is masked as if the
    owner were maximally strict, never under the (permissive) requester's policy alone."""
    db = _digest_db([], scan=RuntimeError("scan probe failed"))
    masked, detector = _mask_digest(db, CONTENT, resolve=lambda *_a, **_k: _permissive())

    assert detector.call_count == 1
    assert PHONE not in masked.content
    assert masked.content.startswith("Call Dana at ")
    assert masked.was_masked is True


def test_a_digest_scan_that_skipped_a_relied_on_detector_is_masked_inline():
    """``done`` without ``pii`` coverage: the cached spans (empty here) cannot be
    trusted for a policy that masks PII, so the inline detector runs instead."""
    db = _digest_db([[_segment(CONTENT, [])]], scan=_scan_row(coverage=["profanity", "toxicity"]))
    masked, detector = _mask_digest(db, CONTENT)

    assert detector.call_count == 1
    assert PHONE not in masked.content
    assert masked.content.startswith("Call Dana at ")


# ------------------------------------------------------- chunk gather failure


def test_a_chunk_whose_gather_raises_is_withheld_whole():
    """A chunk fails closed WHOLE, and the failed gather counts as "masking applies":
    the requester's permissive policy alone must not pass it through."""
    db = MagicMock()
    db.query.side_effect = RuntimeError("scan probe failed")
    with patch(_RESOLVE, side_effect=lambda *_a, **_k: _permissive()):
        masked = mask_chunks(_factory(db), [_chunk()], user_id=REQUESTER_ID)

    assert masked[0].content == ""
    assert masked[0].was_masked is True
