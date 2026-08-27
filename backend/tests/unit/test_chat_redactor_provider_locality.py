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
from app.services.redaction.config import EffectiveRedactionConfig
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


def _db_with(status, segments, coverage=None, language="en", user_id=1):
    """Same shape as ``test_chat_redactor.py``'s helper of the same name — kept

    local rather than imported, per this repo's "new test files stay
    self-contained" convention for parallel-lane work. ``user_id`` self-owns
    the file (task #40, strictest-wins) so every existing call in this module
    (all masking as ``user_id=1``) unions to a no-op, unaffected by #40.
    """
    db = MagicMock()
    scan_q = MagicMock()
    scan_q.filter.return_value.scalar.return_value = status
    scan_q.filter.return_value.first.return_value = SimpleNamespace(
        id=1,
        redaction_status=status,
        redaction_coverage=coverage,
        language=language,
        user_id=user_id,
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
        id=6, redaction_status="done", redaction_coverage=None, language="en", user_id=1
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
    """A DB-shaped session that answers any query with an auto-vivified MagicMock.

    Used to be a literal ``yield None``: before task #40, ``mask_chunks`` never
    touched the session at all when the (mocked) policy disabled masking, so a
    real ``None`` stayed safely unused. Strictest-wins changed that — even a
    fully-disabled REQUESTER policy must still look up the file's OWNER before
    deciding not to mask, so the masking phase now always issues at least one
    ``db.query(...)`` on this session. A ``MagicMock`` answers that safely (every
    attribute/call auto-vivifies to another mock rather than raising), and
    ``resolve_effective_config`` stays patched below to the same canned config
    for every user id it's called with, so the union is a no-op either way —
    this test is still exercising the `llm=None` call site, not real Postgres.
    """
    yield MagicMock()


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
    # Phase 3.5 (quarantine drop) would run a real Postgres query; it is stubbed
    # below regardless of what the session yields, matching
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


# --------------------------------------------------------------------------- #
# Layer A: the resolver + both kwargs builders (`chat/service.py`)
#
# `_prepare_context`'s D6 test above proves the call site survives `llm=None`;
# these prove the RESOLVER itself distinguishes local from remote before any
# of the four call sites below are reached.
# --------------------------------------------------------------------------- #

_LOCAL_LLM = SimpleNamespace(
    config=SimpleNamespace(provider="vllm", base_url="http://localhost:8012/v1")
)
_REMOTE_LLM = SimpleNamespace(config=SimpleNamespace(provider="openai", base_url=None))


def test_a_local_llm_config_resolves_the_exemption():
    from app.services.chat.service import _unmask_for_local

    assert _unmask_for_local(_LOCAL_LLM) is True
    assert _unmask_for_local(_REMOTE_LLM) is False


def test_both_kwargs_builders_carry_the_exemption():
    from app.services.chat.service import _build_digest_mask_kwargs
    from app.services.chat.service import _build_mask_kwargs

    settings = ChatSettings()
    assert _build_mask_kwargs(_LOCAL_LLM, settings) == {"unmask_for_local": True}
    assert _build_mask_kwargs(_REMOTE_LLM, settings) == {}
    assert _build_digest_mask_kwargs(_LOCAL_LLM) == {"unmask_for_local": True}
    assert _build_digest_mask_kwargs(_REMOTE_LLM) == {}


# --------------------------------------------------------------------------- #
# Layer B: all FOUR masker call sites reachable from `_prepare_context`
# actually receive the resolved kwarg. Mock-only on its own (audited below,
# together with Layer C's real-content assertions, which is what discharges
# that finding) — reuses the `_null_session`/`retrieve_context`/
# `_drop_quarantined_hits` scaffold `test_prepare_context_completes_with_no_
# llm_configured` above already established.
# --------------------------------------------------------------------------- #


def _recording_masker(recorded: list[tuple[str, dict]], label: str):
    """A masker stand-in that records its kwargs and returns real MaskedChunks

    (not raw hits) so downstream code that reads `.content`/`.source` off the
    return value — `_emit_expansion`, `build_file_summaries` — keeps working.
    """
    from app.services.chat.redactor import MaskedChunk

    def _fn(_session_factory, items, _user_id, **kwargs):
        recorded.append((label, kwargs))
        return [MaskedChunk(source=item, content=item.content, was_masked=False) for item in items]

    return _fn


def _drive_prepare_context(
    monkeypatch, *, llm, decision, chunks=None, digests=None, file_uuids=None
):
    """Run the real `_prepare_context` with retrieval/routing stubbed and the

    two maskers replaced by recorders. Returns the recorded ``(site, kwargs)``
    list. Shares the D6 test's `_null_session` scaffold above.
    """
    from app.services.chat import router as chat_router
    from app.services.chat import service as chat_service
    from app.services.chat.retrieval import RetrievalResult

    recorded: list[tuple[str, dict]] = []

    monkeypatch.setattr(chat_router, "route", lambda *_a, **_kw: decision)
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(
        chat_service,
        "retrieve_context",
        lambda **_kwargs: RetrievalResult(
            chunks=chunks or [], digests=digests or [], retrieved=len(chunks or [])
        ),
    )
    monkeypatch.setattr(chat_service, "_drop_quarantined_hits", lambda _db, hits: hits)
    monkeypatch.setattr(
        "app.services.redaction.config.resolve_effective_config",
        lambda db, user_id: _cfg(enabled=False, redact_before_llm=False),
    )
    monkeypatch.setattr(chat_service, "mask_chunks", _recording_masker(recorded, "chunk"))
    monkeypatch.setattr(
        "app.services.chat.redactor.mask_digests", _recording_masker(recorded, "digest")
    )

    chat_service._prepare_context(
        user_id=1,
        organization_id=None,
        question="What did they decide?",
        history=[],
        settings=ChatSettings(),
        file_uuids=file_uuids,
        speakers=list(decision.speakers) or None,
        search_mode="hybrid",
        llm=llm,
        rewrite_enabled=False,
    )
    return recorded


def _assert_all_recorded(recorded, *, expect_local: bool, expected_count: int):
    assert len(recorded) == expected_count
    for _site, kwargs in recorded:
        if expect_local:
            assert kwargs.get("unmask_for_local") is True
        else:
            assert "unmask_for_local" not in kwargs


@pytest.mark.parametrize("llm,expect_local", [(_LOCAL_LLM, True), (_REMOTE_LLM, False)])
def test_chunk_and_ranked_digest_sites_receive_the_local_exemption(monkeypatch, llm, expect_local):
    """Sites 1 and 2: `mask_chunks` and the ranked-digest `mask_digests` call.

    Route stays at its default (chunk tier only, no digest routing), so
    `_resolve_summary_tier` falls straight to the already-masked ranked leg
    and issues no THIRD masker call — this test isolates exactly these two
    sites.
    """
    from app.services.chat.router import Route

    hit = _chunk("plain content")
    digest_hit = _digest_hit("plain digest content")
    recorded = _drive_prepare_context(
        monkeypatch, llm=llm, decision=Route(), chunks=[hit], digests=[digest_hit], file_uuids=None
    )
    _assert_all_recorded(recorded, expect_local=expect_local, expected_count=2)
    assert {site for site, _ in recorded} == {"chunk", "digest"}


@pytest.mark.parametrize("llm,expect_local", [(_LOCAL_LLM, True), (_REMOTE_LLM, False)])
def test_speaker_scope_map_site_receives_the_local_exemption(monkeypatch, llm, expect_local):
    """Site 3: the speaker-scoped map inside `_resolve_summary_tier`

    (``Route.wants_speaker_digest_map``). Mutually exclusive with site 4 below
    — a speaker-scoped summarize turn never also runs the plain scope map.
    """
    from app.services.chat import mapreduce as chat_mapreduce
    from app.services.chat.mapreduce import DigestScopeHits
    from app.services.chat.router import INTENT_SUMMARIZE
    from app.services.chat.router import TIER_CHUNK
    from app.services.chat.router import Route

    hit = _chunk("plain content")
    map_hit = _digest_hit("plain map content")
    decision = Route(intent=INTENT_SUMMARIZE, tiers=(TIER_CHUNK,), speakers=("Dana",))
    assert decision.wants_speaker_digest_map is True
    assert decision.wants_digest is False  # precondition: no second digest-plane call

    monkeypatch.setattr(
        chat_mapreduce,
        "scope_speaker_digest_hits",
        lambda *_a, **_kw: DigestScopeHits([map_hit], {}),
    )
    recorded = _drive_prepare_context(
        monkeypatch, llm=llm, decision=decision, chunks=[hit], digests=[], file_uuids=["uuid-1"]
    )
    _assert_all_recorded(recorded, expect_local=expect_local, expected_count=2)
    assert {site for site, _ in recorded} == {"chunk", "digest"}


@pytest.mark.parametrize("llm,expect_local", [(_LOCAL_LLM, True), (_REMOTE_LLM, False)])
def test_scope_map_site_receives_the_local_exemption(monkeypatch, llm, expect_local):
    """Site 4: the bounded-scope map inside `_resolve_summary_tier`

    (``Route.wants_digest`` and a bounded ``file_uuids``, no speaker filter).
    """
    from app.services.chat import mapreduce as chat_mapreduce
    from app.services.chat.mapreduce import DigestScopeHits
    from app.services.chat.router import TIER_CHUNK
    from app.services.chat.router import TIER_DIGEST
    from app.services.chat.router import Route

    hit = _chunk("plain content")
    map_hit = _digest_hit("plain map content")
    decision = Route(tiers=(TIER_CHUNK, TIER_DIGEST))
    assert decision.wants_digest is True
    assert decision.wants_speaker_digest_map is False  # precondition: no site-3 call instead

    monkeypatch.setattr(
        chat_mapreduce, "scope_digest_hits", lambda *_a, **_kw: DigestScopeHits([map_hit], {})
    )
    recorded = _drive_prepare_context(
        monkeypatch, llm=llm, decision=decision, chunks=[hit], digests=[], file_uuids=["uuid-1"]
    )
    _assert_all_recorded(recorded, expect_local=expect_local, expected_count=2)
    assert {site for site, _ in recorded} == {"chunk", "digest"}


# --------------------------------------------------------------------------- #
# Layer C: the EFFECT, not just the flag. The real `mask_chunks`/`mask_digests`
# run (no masker stubbing) against text carrying REAL detectable PII — per
# `redaction/CLAUDE.md`'s warning, "555-1234" is NOT Presidio-detectable and
# would pass this test whether or not masking ran, so this uses a real email
# address instead. Together with Layer B these discharge the mock-only
# finding `scripts/audit-tests.py` would otherwise raise against Layer B alone.
# --------------------------------------------------------------------------- #

_PII_TEXT = "Email me at alice@example.com to confirm"


def _real_cfg(*, redact_before_llm_locked: bool) -> EffectiveRedactionConfig:
    """A REAL ``EffectiveRedactionConfig`` (not the SimpleNamespace ``_cfg``

    stand-in above) — ``RedactionService.mask_segment`` reads ``cfg.allowlist``
    and other fields the stand-in never populated, so using it here would have
    every call fail closed to `""` regardless of masking, which proves nothing
    about genuine detection either way.
    """
    from app.core.constants import DEFAULT_REDACTION_PII_ENTITIES

    return EffectiveRedactionConfig(
        enabled=True,
        enabled_categories={"pii"},
        pii_entities=set(DEFAULT_REDACTION_PII_ENTITIES),
        redact_before_llm=True,
        redact_before_llm_locked=redact_before_llm_locked,
    )


def _db_no_cached_spans(status="done", *, user_id=1):
    """Forces the INLINE masking path (real Presidio, not a cached-span rebuild)

    — the same shape `test_the_inline_detector_runs_with_no_db_session_open`
    documents ("no segments -> inline path") in `test_chat_redactor.py`.
    """
    return _db_with(status, [], user_id=user_id)


def test_a_local_turn_actually_leaves_chunk_text_unmasked():
    """The real masker, not a stub: a local turn's chunk keeps its PII verbatim;

    the remote arm (same setup, same PII, only the flag differs) is the
    control — without it a masker that never masks anything would also pass.
    """
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_real_cfg(redact_before_llm_locked=False),
    ):
        local_masked = mask_chunks(
            _factory(_db_no_cached_spans()), [_chunk(_PII_TEXT)], user_id=1, unmask_for_local=True
        )
        remote_masked = mask_chunks(
            _factory(_db_no_cached_spans()), [_chunk(_PII_TEXT)], user_id=1, unmask_for_local=False
        )

    assert "alice@example.com" in local_masked[0].content
    assert "alice@example.com" not in remote_masked[0].content


def test_the_admin_lock_overrides_the_local_exemption_for_chunks():
    """`redact_before_llm_locked=True` + a local provider still masks — the

    force floor beats the per-provider exemption, for this deployment's own
    vLLM/Ollama included.
    """
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_real_cfg(redact_before_llm_locked=True),
    ):
        masked = mask_chunks(
            _factory(_db_no_cached_spans()), [_chunk(_PII_TEXT)], user_id=1, unmask_for_local=True
        )

    assert "alice@example.com" not in masked[0].content


def _db_digest_no_cached_provenance(status="pending", *, user_id=1):
    """Forces `_gather_digest_plans`'s ``sentences is None`` branch (the digest

    inline path) by failing the ``redaction_status == done`` gate: the batched
    ``file_facts`` read happens first, then one scan lookup per hit, and no
    further query runs once the status check fails the gate.
    """
    db = MagicMock()
    facts_q = MagicMock()
    facts_q.filter.return_value.all.return_value = []
    scan_q = MagicMock()
    scan_q.filter.return_value.first.return_value = SimpleNamespace(
        id=6, redaction_status=status, redaction_coverage=None, language="en", user_id=user_id
    )
    db.query.side_effect = [facts_q, scan_q]
    return db


def test_a_local_turn_actually_leaves_digest_text_unmasked():
    """The digest-plane counterpart: real `mask_digests`, real PII, a remote

    control.
    """
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_real_cfg(redact_before_llm_locked=False),
    ):
        local_masked = mask_digests(
            _factory(_db_digest_no_cached_provenance()),
            [_digest_hit(_PII_TEXT)],
            user_id=1,
            unmask_for_local=True,
        )
        remote_masked = mask_digests(
            _factory(_db_digest_no_cached_provenance()),
            [_digest_hit(_PII_TEXT)],
            user_id=1,
            unmask_for_local=False,
        )

    assert "alice@example.com" in local_masked[0].content
    assert "alice@example.com" not in remote_masked[0].content


def test_the_admin_lock_overrides_the_local_exemption_for_digests():
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_real_cfg(redact_before_llm_locked=True),
    ):
        masked = mask_digests(
            _factory(_db_digest_no_cached_provenance()),
            [_digest_hit(_PII_TEXT)],
            user_id=1,
            unmask_for_local=True,
        )

    assert "alice@example.com" not in masked[0].content
