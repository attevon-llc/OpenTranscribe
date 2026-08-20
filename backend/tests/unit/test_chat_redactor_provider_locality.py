"""``unmask_for_local`` — the provider-keyed masking exemption in `redactor._gather`.

Owner decision, 2026-08-13 (see ``services/chat/CLAUDE.md``): a LOCAL model never has
excerpt text leave the machine, so masking it before the call is pure recall cost with
no egress benefit. ``chat/service.py`` resolves
``redaction.llm_guard.is_local_provider(llm.config)`` once per turn and threads it into
``mask_chunks``/``mask_digests`` as ``unmask_for_local`` — this module is the redactor
half of that contract: is the exemption actually applied, and does the admin force
floor (``cfg.redact_before_llm_locked``) correctly override it.

``is_local_provider`` itself is pinned in ``test_llm_provider_locality.py``; this
module never re-derives that classification, it drives the masker with the answer
already decided, exactly the way ``chat/service.py`` calls it.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.chat.redactor import mask_chunks
from app.services.chat.redactor import mask_digests
from app.services.chat.settings import ChatSettings
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


@contextmanager
def _one_session(db):
    yield db


def _factory(db):
    return lambda: _one_session(db)


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


def _digest_hit(content: str = "We agreed the budget.") -> ChunkHit:
    return ChunkHit(
        file_uuid="22222222-2222-2222-2222-222222222222",
        file_id=6,
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
    redact_before_llm_locked: bool = False,
    categories=("pii", "profanity", "custom"),
):
    """Stand-in for ``EffectiveRedactionConfig``, including the force-floor field

    the local exemption reads. Every existing test in ``test_chat_redactor.py`` /
    ``test_chat_digest_masking.py`` calls with ``unmask_for_local`` defaulted to
    False, so `_gather` never reads `redact_before_llm_locked` for them — this
    field only matters here, where the exemption is actually exercised.
    """
    return SimpleNamespace(
        enabled=enabled,
        redact_before_llm=redact_before_llm,
        redact_before_llm_locked=redact_before_llm_locked,
        enabled_categories=set(categories),
    )


# --------------------------------------------------------------------------- #
# mask_chunks — the full matrix
# --------------------------------------------------------------------------- #


def test_local_provider_skips_masking_when_the_policy_would_otherwise_apply():
    """The exemption: unmask_for_local=True, no admin lock -> content passes through.

    `db` is never queried — `_gather` returns before `_gather_chunk_plans` runs,
    because `applies` is already False by the time the DB phase would start. A
    bare MagicMock proves that: any unexpected `.query()` call would return
    another MagicMock rather than raising, so if this test passed only because
    a query silently no-opped, `masked[0].content` would not equal the ORIGINAL
    unmasked chunk content — but it does, which pins the true code path.
    """
    db = MagicMock()
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=True, redact_before_llm=True, redact_before_llm_locked=False),
    ):
        masked = mask_chunks(_factory(db), [_chunk()], user_id=1, unmask_for_local=True)

    assert masked[0].content == "my number is 555-1234"
    assert masked[0].was_masked is False
    db.query.assert_not_called()


def test_unmask_for_local_defaults_to_false_and_still_masks():
    """The control: every EXISTING caller that never passes the kwarg is unaffected."""
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
        ),
    ):
        masked = mask_chunks(_factory(db), [_chunk()], user_id=1)  # no unmask_for_local at all

    assert masked[0].content == "my number is [PHONE]"
    assert masked[0].was_masked is True


def test_admin_force_floor_overrides_the_local_exemption():
    """The required 7th case: force-floor locked + local -> MASKED.

    Same local provider as the first test, but the admin has forced
    `redact_before_llm`. The floor must win: masking still applies to this
    deployment's own vLLM/Ollama, not just to external providers.
    """
    segment = SimpleNamespace(
        text="my number is 555-1234",
        redactions=[{"char_start": 13, "char_end": 21, "category": "pii", "entity_type": "PHONE"}],
        words=None,
    )
    db = _db_with("done", [segment])

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True, redact_before_llm_locked=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            return_value=("my number is [PHONE]", []),
        ) as mask_segment,
    ):
        masked = mask_chunks(_factory(db), [_chunk()], user_id=1, unmask_for_local=True)

    assert masked[0].content == "my number is [PHONE]"
    assert masked[0].was_masked is True
    assert "555-1234" not in masked[0].content
    assert mask_segment.called


def test_the_policy_off_case_is_unaffected_by_the_local_flag():
    """A policy that would never mask anyway is not moved by `unmask_for_local`

    either way — the exemption only has an effect when masking would otherwise
    apply, per `_gather`'s `applies and unmask_for_local and not locked` guard.
    """
    db = MagicMock()
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=True, redact_before_llm=False),
    ):
        masked_true = mask_chunks(_factory(db), [_chunk()], user_id=1, unmask_for_local=True)
        masked_false = mask_chunks(_factory(db), [_chunk()], user_id=1, unmask_for_local=False)

    assert masked_true[0].content == masked_false[0].content == "my number is 555-1234"
    assert masked_true[0].was_masked is masked_false[0].was_masked is False


def _db_with(status, segments, coverage=None, language="en"):
    """Same shape as ``test_chat_redactor.py``'s helper of the same name — kept

    local rather than imported, per this repo's "new test files stay
    self-contained" convention for parallel-lane work.
    """
    db = MagicMock()
    scan_q = MagicMock()
    scan_q.filter.return_value.scalar.return_value = status
    scan_q.filter.return_value.first.return_value = SimpleNamespace(
        id=1, redaction_status=status, redaction_coverage=coverage, language=language
    )
    seg_q = MagicMock()
    seg_q.filter.return_value.order_by.return_value.all.return_value = segments
    db.query.side_effect = [scan_q, seg_q]
    return db


# --------------------------------------------------------------------------- #
# mask_digests — same `_gather` gate, threaded through the digest entry point
# --------------------------------------------------------------------------- #


def test_mask_digests_also_skips_for_a_local_provider():
    """Proves the parameter reaches `_gather` from the DIGEST entry point too —

    `mask_chunks` and `mask_digests` are two callers of one shared gate, and a
    change that wired the kwarg into only one of them would leave chat's
    digest/summary tier masking a local model's own excerpts for no reason.
    """
    db = MagicMock()
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=True, redact_before_llm=True, redact_before_llm_locked=False),
    ):
        masked = mask_digests(_factory(db), [_digest_hit()], user_id=1, unmask_for_local=True)

    assert masked[0].content == "We agreed the budget."
    assert masked[0].was_masked is False
    db.query.assert_not_called()


def test_mask_digests_respects_the_force_floor_too():
    """The digest counterpart of the chunk force-floor test above.

    Provenance fixture mirrors ``test_chat_digest_masking.py``'s shape: one
    section, one sentence, drawn from one segment.
    """
    from app.services.ingest_artifacts.provenance import KIND_SEGMENT_IDS

    sentence = {
        "text": "We agreed the budget.",
        "order": 0,
        "speaker": "Dana",
        "provenance": {
            "kind": KIND_SEGMENT_IDS,
            "segment_ids": [101],
            "start_time": 12.5,
            "end_time": 20.0,
        },
    }
    digest_row = SimpleNamespace(digest={"sections": [{"index": 0, "sentences": [sentence]}]})
    segment_row = SimpleNamespace(
        id=101, text="We agreed the budget.", redactions=[], words=None, start_time=12.5
    )

    db = MagicMock()
    scan_q = MagicMock()
    scan_q.filter.return_value.first.return_value = SimpleNamespace(
        id=6, redaction_status="done", redaction_coverage=None, language="en"
    )
    facts_q = MagicMock()
    facts_q.filter.return_value.first.return_value = (digest_row.digest,)
    seg_q = MagicMock()
    seg_q.filter.return_value.order_by.return_value.all.return_value = [segment_row]
    db.query.side_effect = [scan_q, facts_q, seg_q]

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True, redact_before_llm_locked=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            return_value=("[REDACTED BUDGET SENTENCE]", []),
        ),
    ):
        masked = mask_digests(_factory(db), [_digest_hit()], user_id=1, unmask_for_local=True)

    assert masked[0].content == "[REDACTED BUDGET SENTENCE]"
    assert masked[0].was_masked is True


# --------------------------------------------------------------------------- #
# `_prepare_context` with `llm=None` — issue #403 D6: no LLM_PROVIDER configured
# is a first-class deployment, and it must not crash while resolving provider
# locality for masking. Regression test for the exact defect: an earlier
# version of the `unmask_for_local` wiring read `llm.config` unconditionally,
# raising `AttributeError: 'NoneType' object has no attribute 'config'` the
# moment ANY chat turn ran with no LLM configured — which every deterministic
# (no-LLM) turn does.
# --------------------------------------------------------------------------- #


@contextmanager
def _null_session():
    yield None


def test_prepare_context_completes_with_no_llm_configured(monkeypatch):
    """D6: a deployment with no LLM_PROVIDER must still produce masked context."""
    from app.services.chat import service as chat_service
    from app.services.chat.retrieval import RetrievalResult

    hit = _chunk("plain content, nothing to detect")
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(
        chat_service,
        "retrieve_context",
        lambda **_kwargs: RetrievalResult(chunks=[hit], retrieved=1),
    )
    # Phase 3.5 (quarantine drop) runs a real Postgres query; this test's session
    # factory yields `None` on purpose to stay Postgres-free, matching
    # `test_chat_masking_diagnostics.py`'s `_prepare` helper.
    monkeypatch.setattr(chat_service, "_drop_quarantined_hits", lambda _db, hits: hits)
    # The real `mask_chunks`/`redactor._gather` run for real here (not stubbed)
    # — the point of this test is that the CALL SITE in `_prepare_context`
    # survives `llm=None` when computing `unmask_for_local`, not that masking
    # itself is exercised again (that is every other test in this module).
    monkeypatch.setattr(
        "app.services.redaction.config.resolve_effective_config",
        lambda db, user_id: SimpleNamespace(
            enabled=False,
            redact_before_llm=False,
            redact_before_llm_locked=False,
            enabled_categories=set(),
        ),
    )

    masked, meta, counted, overview, _synthesis, _recurrence = chat_service._prepare_context(
        user_id=1,
        organization_id=None,
        question="What did they decide?",
        history=[],
        settings=ChatSettings(),
        file_uuids=None,
        speakers=None,
        search_mode="hybrid",
        llm=None,
        rewrite_enabled=False,
    )

    assert counted is None
    assert overview is None
    assert len(masked) == 1
    assert masked[0].content == "plain content, nothing to detect"
