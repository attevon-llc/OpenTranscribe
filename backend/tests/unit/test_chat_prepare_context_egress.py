"""The egress guarantee through ``_prepare_context``: a remote turn hands on MASKED text.

``test_chat_redactor_provider_locality.py`` proves each masker call receives the right
``unmask_for_local`` kwarg (its Layer B) and that each masker honours it (Layer C).
Neither proves ``_prepare_context`` then returns the MASKER'S OUTPUT rather than the raw
retrieved text: a refactor that kept both masker calls but built the returned evidence
from ``result.chunks`` / ``result.digests`` passes both and sends unredacted transcript
text to a remote provider. Measured: that mutation of the ranked-digest leg stayed green
across the locality, digest-masking and masking-diagnostics suites.

Here the real ``mask_chunks`` runs (cached-span path; ``mask_segment`` stands in for the
replacement the detector would make) and the local provider is the control, so the
remote arm cannot pass merely because the fixture never carried raw text.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.chat import service as chat_service
from app.services.chat.redactor import MaskedChunk
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.settings import ChatSettings
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit

_LOCAL_LLM = SimpleNamespace(
    config=SimpleNamespace(provider="vllm", base_url="http://localhost:8012/v1")
)
_REMOTE_LLM = SimpleNamespace(config=SimpleNamespace(provider="openai", base_url=None))
_CHUNK_RAW = "my number is 555-1234"
_DIGEST_RAW = "The phone number 555-1234 was agreed."


def _chunk() -> ChunkHit:
    return ChunkHit(
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=5,
        chunk_index=0,
        content=_CHUNK_RAW,
        title="Call",
        speaker="Dana",
        start_time=10.0,
        end_time=40.0,
    )


def _digest_hit() -> ChunkHit:
    return ChunkHit(
        file_uuid="22222222-2222-2222-2222-222222222222",
        file_id=6,
        chunk_index=-1,
        content=_DIGEST_RAW,
        title="Weekly sync",
        speaker=None,
        start_time=12.5,
        end_time=3400.0,
        digest_section=0,
    )


def _segment_db():
    """One scan row (done, self-owned) and one segment carrying a cached PHONE span."""
    segment = SimpleNamespace(
        text=_CHUNK_RAW,
        redactions=[{"char_start": 13, "char_end": 21, "category": "pii", "entity_type": "PHONE"}],
        words=None,
    )
    db = MagicMock()
    scan_q = MagicMock()
    scan_q.filter.return_value.scalar.return_value = "done"
    scan_q.filter.return_value.first.return_value = SimpleNamespace(
        id=1, redaction_status="done", redaction_coverage=None, language="en", user_id=1
    )
    seg_q = MagicMock()
    seg_q.filter.return_value.order_by.return_value.all.return_value = [segment]
    db.query.side_effect = [scan_q, seg_q]
    return db


@contextmanager
def _fresh_segment_session():
    """Every phase's short session gets its own two-query masking fixture."""
    yield _segment_db()


def _digest_masker(_factory, hits, _user_id, **kwargs):
    local = kwargs.get("unmask_for_local") is True
    return [MaskedChunk(source=h, content=h.content if local else "[DIGEST MASKED]") for h in hits]


def _drive_real_chunk_masking(monkeypatch, *, llm) -> list[str]:
    """Real `_prepare_context` + real `mask_chunks`; retrieval and quarantine stubbed."""
    monkeypatch.setattr("app.db.session_utils.session_scope", _fresh_segment_session)
    monkeypatch.setattr(
        "app.services.chat.language.supported_rag_languages", lambda _db: frozenset({"en"})
    )
    monkeypatch.setattr(
        chat_service,
        "retrieve_context",
        lambda **_kwargs: RetrievalResult(chunks=[_chunk()], digests=[_digest_hit()], retrieved=1),
    )
    monkeypatch.setattr(chat_service, "_drop_quarantined_hits", lambda _db, hits: hits)
    monkeypatch.setattr(
        "app.services.redaction.config.resolve_effective_config",
        lambda *_a, **_k: SimpleNamespace(
            enabled=True,
            redact_before_llm=True,
            redact_before_llm_locked=False,
            enabled_categories={"pii", "profanity", "custom"},
        ),
    )
    monkeypatch.setattr(
        "app.services.redaction.service.RedactionService.mask_segment",
        lambda *_a, **_k: ("my number is [PHONE]", []),
    )
    monkeypatch.setattr("app.services.chat.redactor.mask_digests", _digest_masker)

    masked, *_rest = chat_service._prepare_context(
        user_id=1,
        organization_id=None,
        question="What number did they give?",
        history=[],
        settings=ChatSettings(context_expansion_enabled=False),
        file_uuids=None,
        speakers=None,
        search_mode="hybrid",
        llm=llm,
        rewrite_enabled=False,
    )
    return [m.content for m in masked]


def test_prepare_context_returns_masked_text_for_a_remote_provider(monkeypatch):
    """Nothing raw survives a remote turn, on either plane."""
    contents = _drive_real_chunk_masking(monkeypatch, llm=_REMOTE_LLM)

    assert contents == ["[DIGEST MASKED]", "my number is [PHONE]"]
    assert not any("555-1234" in content for content in contents)


def test_prepare_context_returns_raw_text_for_a_local_provider(monkeypatch):
    """The control: same fixture, local provider, so the raw text IS reachable."""
    contents = _drive_real_chunk_masking(monkeypatch, llm=_LOCAL_LLM)

    assert contents == [_DIGEST_RAW, _CHUNK_RAW]
