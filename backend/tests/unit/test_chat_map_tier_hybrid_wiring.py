"""#532 follow-up (Unit U4): wiring, instrumentation, and the mandatory
citation guard for the hybrid map-tier entry.

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


def _closing_hit(uuid: str, file_id: int, *, content: str) -> ChunkHit:
    return ChunkHit(
        file_uuid=uuid,
        file_id=file_id,
        chunk_index=-2,
        content=content,
        title="Weekly sync",
        start_time=60.0,
        end_time=90.0,
        digest_section=1,
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


def _hybrid_map_hits() -> DigestScopeHits:
    hits = [
        _summary_hit("uuid-1", 1, content="Abstractive lead paragraph."),
        _closing_hit("uuid-1", 1, content="Closing section verbatim."),
    ]
    coverage = {
        "files_without_artifacts": 0,
        "files_no_content": 0,
        "summary_hits": 1,
        "hybrid_entries": 1,
        "summary_only_entries": 0,
        "entries_digest": 0,
        "summary_chars": len("Abstractive lead paragraph."),
        "closing_section_chars": len("Closing section verbatim."),
    }
    return DigestScopeHits(hits, coverage)


def _digest_only_map_hits() -> DigestScopeHits:
    hits = [_digest_hit("uuid-1", 1, content="Plain digest text.")]
    coverage = {
        "files_without_artifacts": 0,
        "files_no_content": 0,
        "summary_hits": 0,
        "hybrid_entries": 0,
        "summary_only_entries": 0,
        "entries_digest": 1,
        "summary_chars": 0,
        "closing_section_chars": 0,
    }
    return DigestScopeHits(hits, coverage)


# --------------------------------------------------------------------------- #
# Instrumentation (meta["overview"] diagnostics)
# --------------------------------------------------------------------------- #


def test_a_hybrid_turn_reports_the_new_overview_counts(monkeypatch):
    settings = ChatSettings(map_tier_summaries=True, map_tier_hybrid=True)
    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch, settings=settings, map_hits=_hybrid_map_hits()
    )

    assert overview is not None
    assert "Abstractive lead paragraph." in overview.block
    assert "Closing section verbatim." in overview.block
    diag = meta["overview"]
    assert diag["entries_hybrid"] == 1
    assert diag["entries_summary_only"] == 0
    assert diag["entries_digest"] == 0
    assert diag["block_chars"] == len(overview.block)
    assert diag["summary_chars"] == len("Abstractive lead paragraph.")
    assert diag["digest_chars"] == len("Closing section verbatim.")
    assert diag["closing_in_ranked_digests"] == 0, "the ranked leg found nothing this turn"


def test_a_flag_off_turn_reports_zero_hybrid_entries(monkeypatch):
    """Control: with the flag off, the same scope-map leg reports
    ``entries_hybrid == 0`` — never absent, so a reader reconciling coverage
    sees an explicit zero rather than a silent gap."""
    settings = ChatSettings(map_tier_summaries=False, map_tier_hybrid=False)
    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch, settings=settings, map_hits=_digest_only_map_hits()
    )

    assert overview is not None
    diag = meta["overview"]
    assert diag["entries_hybrid"] == 0
    assert diag["entries_digest"] == 1


def test_closing_in_ranked_digests_counts_an_overlap_with_the_ranked_leg(monkeypatch):
    """A hybrid file's closing section that ALSO appears in this turn's ranked
    digest leg (same file_uuid, same digest_section) is counted, not deduplicated
    away — plan section 2.3's "duplicate avoidance is not attempted" note."""
    settings = ChatSettings(map_tier_summaries=True, map_tier_hybrid=True)
    ranked_overlap = ChunkHit(
        file_uuid="uuid-1",
        file_id=1,
        chunk_index=5,
        content="Closing section verbatim, ranked separately.",
        title="Weekly sync",
        digest_section=1,
    )
    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch, settings=settings, map_hits=_hybrid_map_hits(), digests=[ranked_overlap]
    )

    assert meta["overview"]["closing_in_ranked_digests"] == 1


# --------------------------------------------------------------------------- #
# The mandatory citation guard (plan section 2.7)
# --------------------------------------------------------------------------- #


def test_hybrid_plus_overview_citable_suppresses_citation_ids(monkeypatch):
    """A hybrid entry's closing section is verbatim transcript text. With
    ``overview_citable`` also on, the guard must fire: no citation ids
    assigned, and the suppression is recorded on meta rather than silently
    dropped."""
    settings = ChatSettings(map_tier_summaries=True, map_tier_hybrid=True, overview_citable=True)
    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch, settings=settings, map_hits=_hybrid_map_hits()
    )

    assert overview.cited_entries == ()
    assert overview.citation_payloads == ()
    assert meta["overview_citable_suppressed"] == "hybrid"
    assert "[" not in overview.block.split("- ")[1].split("\n")[0], (
        "no citation id rendered into the listed-file line"
    )


def test_control_overview_citable_with_a_digest_only_map_still_assigns_ids(monkeypatch):
    """MUST-STAY-CLEAN control: a digest-only (non-hybrid) map still gets
    citation ids under ``overview_citable`` — the guard is specific to a
    hybrid entry, not a blanket suppression of the whole #532 arm (a)."""
    settings = ChatSettings(map_tier_summaries=False, overview_citable=True)
    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch, settings=settings, map_hits=_digest_only_map_hits()
    )

    assert len(overview.cited_entries) == 1
    assert "overview_citable_suppressed" not in meta
