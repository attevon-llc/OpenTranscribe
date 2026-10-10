"""#532 (Unit U4): the content-free ``meta["overview"]`` diagnostics.

Same harness shape as ``test_chat_map_decoupling.py`` (W2.1's sibling suite):
drives the real ``ChatService._prepare_context`` with ``route``,
``retrieve_context``, ``mask_chunks``, ``scope_digest_hits`` and
``mask_digests`` stubbed (network/DB-free); ``build_file_summaries``/
``build_overview`` run for REAL, so assertions are against genuine rendered
``<overview>`` text and genuine ``meta`` counts, not a mocked return value.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.services.chat import service as chat_service
from app.services.chat.mapreduce import DigestScopeHits
from app.services.chat.redactor import MaskedChunk
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.router import Route
from app.services.chat.settings import ChatSettings
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


@contextmanager
def _null_session():
    yield None


def _summarize_route() -> Route:
    return Route(intent="summarize", tiers=("digest", "chunk"))


def _summary_hit(uuid: str, file_id: int, *, content: str) -> ChunkHit:
    return ChunkHit(
        file_uuid=uuid,
        file_id=file_id,
        chunk_index=-1,
        content=content,
        title="Weekly sync",
        start_time=0.0,
        end_time=None,
        digest_section=1,
        is_llm_summary=True,
    )


def _digest_hit(uuid: str, file_id: int, *, content: str) -> ChunkHit:
    return ChunkHit(
        file_uuid=uuid,
        file_id=file_id,
        chunk_index=-1,
        content=content,
        title="Weekly sync",
        digest_section=0,
    )


def _prepare(monkeypatch, *, settings, map_hits, digests=()):
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(chat_service, "_drop_quarantined_hits", lambda _db, hits: hits)
    monkeypatch.setattr("app.services.chat.router.route", lambda *_a, **_k: _summarize_route())
    monkeypatch.setattr(
        chat_service,
        "retrieve_context",
        lambda **_: RetrievalResult(chunks=[], digests=list(digests), retrieved=0),
    )
    monkeypatch.setattr(chat_service, "mask_chunks", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "app.services.chat.redactor.mask_digests",
        lambda _factory, hits, _uid, **_k: [MaskedChunk(source=h, content=h.content) for h in hits],
    )
    monkeypatch.setattr(
        "app.services.chat.mapreduce.scope_digest_hits",
        lambda _db, _uuids, **_kwargs: map_hits,
    )

    return chat_service._prepare_context(
        user_id=1,
        organization_id=None,
        question="summarize what we covered",
        history=[],
        settings=settings,
        file_uuids=["uuid-1"],
        speakers=None,
        search_mode="hybrid",
        llm=None,
        rewrite_enabled=False,
    )


def _summary_only_map_hits() -> DigestScopeHits:
    """The shipped #464 shape: a fresh summary replaces the file's digest."""
    hits = [_summary_hit("uuid-1", 1, content="Abstractive lead paragraph.")]
    coverage = {
        "files_without_artifacts": 0,
        "files_no_content": 0,
        "summary_hits": 1,
        "entries_digest": 0,
    }
    return DigestScopeHits(hits, coverage)


def _digest_only_map_hits() -> DigestScopeHits:
    hits = [_digest_hit("uuid-1", 1, content="Plain digest text.")]
    coverage = {
        "files_without_artifacts": 0,
        "files_no_content": 0,
        "summary_hits": 0,
        "entries_digest": 1,
    }
    return DigestScopeHits(hits, coverage)


# --------------------------------------------------------------------------- #
# Instrumentation (meta["overview"] diagnostics)
# --------------------------------------------------------------------------- #


def test_a_summary_tier_turn_reports_summary_chars_and_no_digest_entries(monkeypatch):
    settings = ChatSettings(map_tier_summaries=True)
    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch, settings=settings, map_hits=_summary_only_map_hits()
    )

    assert overview is not None
    assert "Abstractive lead paragraph." in overview.block
    diag = meta["overview"]
    assert diag["entries_digest"] == 0
    assert diag["block_chars"] == len(overview.block)
    assert diag["summary_chars"] == len("Abstractive lead paragraph.")
    assert diag["digest_chars"] == 0
    assert "entries_hybrid" not in diag, "the hybrid shape was deleted; nothing reports it"


def test_a_digest_tier_turn_reports_digest_chars_and_no_summary_chars(monkeypatch):
    """Control for the test above: the same scope-map leg over a digest-only map
    reports the mirror image, so the counters are not constants."""
    settings = ChatSettings(map_tier_summaries=False)
    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch, settings=settings, map_hits=_digest_only_map_hits()
    )

    assert overview is not None
    diag = meta["overview"]
    assert diag["entries_digest"] == 1
    assert diag["summary_chars"] == 0
    assert diag["digest_chars"] == len("Plain digest text.")


def test_overview_citable_assigns_ids_for_a_digest_only_map(monkeypatch):
    settings = ChatSettings(map_tier_summaries=False, overview_citable=True)
    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch, settings=settings, map_hits=_digest_only_map_hits()
    )

    assert len(overview.cited_entries) == 1
    assert "overview_citable_suppressed" not in meta
