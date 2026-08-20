"""W2.3: ``_resolve_summary_tier`` routes a speaker-scoped summarize turn to
``scope_speaker_digest_hits`` — the fix for the gap ``Route.wants_speaker_
digest_map`` documents.

Drives the real ``ChatService._prepare_context``/``_resolve_summary_tier``
with ``route``, ``retrieve_context``, ``mask_chunks``, ``scope_speaker_digest_
hits`` and ``mask_digests`` stubbed (network/DB-free) — same harness shape as
``test_chat_map_decoupling.py`` (W2.1's sibling suite for the recording-level
map), one level down: a speaker filter this time, not an unranked scope.
``build_file_summaries``/``build_overview`` run for REAL, so assertions are
against genuine rendered ``<overview>`` text.
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


def _speaker_route(speakers=("Dana Whitfield",)) -> Route:
    return Route(intent="summarize", tiers=("chunk",), speakers=tuple(speakers))


def _digest_hit(uuid: str, file_id: int, title: str, speaker: str) -> ChunkHit:
    return ChunkHit(
        file_uuid=uuid,
        file_id=file_id,
        chunk_index=-1,
        content=f"What {speaker} said in {title}.",
        title=title,
        speaker=speaker,
        digest_section=0,
    )


def _prepare(monkeypatch, *, file_uuids, map_hits, route=None, scope_speaker_digest_hits=None):
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(chat_service, "_drop_quarantined_hits", lambda _db, hits: hits)
    monkeypatch.setattr(
        "app.services.chat.router.route", lambda *_a, **_k: route or _speaker_route()
    )
    monkeypatch.setattr(
        chat_service,
        "retrieve_context",
        lambda **_: RetrievalResult(chunks=[], digests=[], retrieved=0),
    )
    monkeypatch.setattr(chat_service, "mask_chunks", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "app.services.chat.redactor.mask_digests",
        lambda _factory, hits, _uid, **_k: [MaskedChunk(source=h, content=h.content) for h in hits],
    )

    calls: list[dict] = []

    def _default(_db, _uuids, _speakers, **kwargs):
        calls.append({"uuids": list(_uuids), "speakers": list(_speakers), **kwargs})
        return map_hits

    monkeypatch.setattr(
        "app.services.chat.mapreduce.scope_speaker_digest_hits",
        scope_speaker_digest_hits or _default,
    )

    result = chat_service._prepare_context(
        user_id=1,
        organization_id=None,
        question="summarize what Dana said",
        history=[],
        settings=ChatSettings(),
        file_uuids=file_uuids,
        speakers=None,
        search_mode="hybrid",
        llm=None,
        rewrite_enabled=False,
    )
    return (*result, calls)


def test_a_speaker_scoped_summarize_calls_the_speaker_map_with_the_route_speakers(monkeypatch):
    map_hits = DigestScopeHits(
        [_digest_hit("uuid-a", 1, "Weekly sync", "Dana Whitfield")],
        {"files_without_artifacts": 0, "files_with_no_speaker_match": 0},
    )
    _masked, meta, _counted, overview, _synthesis, _recurrence, calls = _prepare(
        monkeypatch, file_uuids=["uuid-a"], map_hits=map_hits
    )

    assert len(calls) == 1
    assert calls[0]["uuids"] == ["uuid-a"]
    assert calls[0]["speakers"] == ["Dana Whitfield"]
    assert overview is not None
    assert "Dana Whitfield" in overview.block
    assert meta["map_leg"] == "speaker_scope_map"


def test_it_never_runs_for_an_unscoped_summarize(monkeypatch):
    def _explode(*_a, **_k):
        raise AssertionError("scope_speaker_digest_hits must not run without a speaker filter")

    _masked, meta, _counted, overview, _synthesis, _recurrence, _calls = _prepare(
        monkeypatch,
        file_uuids=["uuid-a"],
        map_hits=DigestScopeHits([], {"files_without_artifacts": 0}),
        route=Route(intent="summarize", tiers=("digest", "chunk")),
        scope_speaker_digest_hits=_explode,
    )
    assert "map_leg" not in meta or meta["map_leg"] != "speaker_scope_map"


def test_never_a_silent_zero_when_the_speaker_map_finds_nothing(monkeypatch):
    """THE property this whole task exists for: an empty map still produces
    an explicit block naming the speaker, not silence."""
    map_hits = DigestScopeHits([], {"files_without_artifacts": 0, "files_with_no_speaker_match": 1})
    _masked, meta, _counted, overview, _synthesis, _recurrence, _calls = _prepare(
        monkeypatch, file_uuids=["uuid-a"], map_hits=map_hits
    )

    assert overview is not None
    assert overview.block != ""
    assert "focus speaker: Dana Whitfield" in overview.block
    assert meta["map_leg"] == "speaker_scope_map_empty"


def test_files_without_artifacts_is_surfaced_in_meta(monkeypatch):
    map_hits = DigestScopeHits(
        [_digest_hit("uuid-a", 1, "Weekly sync", "Dana Whitfield")],
        {"files_without_artifacts": 3, "files_with_no_speaker_match": 0},
    )
    _masked, meta, _counted, _overview, _synthesis, _recurrence, _calls = _prepare(
        monkeypatch, file_uuids=["uuid-a", "uuid-b", "uuid-c", "uuid-d"], map_hits=map_hits
    )

    assert meta["map_files_without_artifacts"] == 3


def test_multi_speaker_scope_is_threaded_through_as_a_list(monkeypatch):
    map_hits = DigestScopeHits(
        [
            _digest_hit("uuid-a", 1, "Weekly sync", "Dana Whitfield"),
        ],
        {"files_without_artifacts": 0, "files_with_no_speaker_match": 0},
    )
    _masked, _meta, _counted, _overview, _synthesis, _recurrence, calls = _prepare(
        monkeypatch,
        file_uuids=["uuid-a"],
        map_hits=map_hits,
        route=_speaker_route(speakers=("Dana Whitfield", "Bo Chen")),
    )

    assert sorted(calls[0]["speakers"]) == ["Bo Chen", "Dana Whitfield"]


def test_unbounded_scope_does_not_run_the_speaker_map(monkeypatch):
    """A speaker-scoped map needs a bounded, enumerated file list — same
    precondition the recording-level scope map documents."""

    def _explode(*_a, **_k):
        raise AssertionError("the speaker map cannot run over an unbounded scope")

    _masked, meta, _counted, overview, _synthesis, _recurrence, _calls = _prepare(
        monkeypatch,
        file_uuids=None,
        map_hits=DigestScopeHits([], {"files_without_artifacts": 0}),
        scope_speaker_digest_hits=_explode,
    )
    assert overview is None
