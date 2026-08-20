"""W2.1: the scope map decouples from the ranked digest leg.

The bug: `_prepare_context` used to run `scope_digest_hits` (the map over
`file_facts`, one entry per file in scope) ONLY when `result.digests` — the
OpenSearch-RANKED digest leg — came back non-empty. So the coverage map died
exactly when it mattered most: a bounded scope whose ranked search simply did
not surface any digest sections. The map is a Postgres read keyed on the
resolved scope, not a consequence of what the ranked leg happened to find, and
this suite pins the decoupling with the ranked leg stubbed to ``[]`` — the red
case named in the review brief.

Drives the real `ChatService._prepare_context` with `route`, `retrieve_context`,
`mask_chunks`, `scope_digest_hits` and `mask_digests` stubbed (network/DB-free);
`build_file_summaries`/`build_overview` run for REAL, so the assertions are
against genuine rendered `<overview>` text, not a mocked return value.
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
    """`_prepare_context` opens its own short sessions; `scope_digest_hits` and
    `mask_digests` are stubbed below so nothing here needs a real Postgres row."""
    yield None


def _summarize_route() -> Route:
    return Route(intent="summarize", tiers=("digest", "chunk"))


def _digest_hit(uuid: str, file_id: int, title: str) -> ChunkHit:
    return ChunkHit(
        file_uuid=uuid,
        file_id=file_id,
        chunk_index=-1,
        content=f"Extractive digest for {title}.",
        title=title,
        digest_section=0,
    )


def _prepare(
    monkeypatch, *, file_uuids, map_hits=None, digests=(), route=None, scope_digest_hits=None
):
    """Shared `_prepare_context` harness. One place to patch the seven seams
    every test here needs, so a test that wants a NON-default route or a
    scope_digest_hits that must not be called (`test_a_lookup_turn_never_
    runs_the_map`) still reads as one call, not a body full of `patch()`s of
    its own (the `mock-heavy` finding `scripts/audit-tests.py` flags a test
    for accumulating directly)."""
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(chat_service, "_drop_quarantined_hits", lambda _db, hits: hits)
    monkeypatch.setattr(
        "app.services.chat.router.route", lambda *_a, **_k: route or _summarize_route()
    )
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

    def _default_scope_digest_hits(_db, _uuids, **_kwargs):
        return map_hits

    monkeypatch.setattr(
        "app.services.chat.mapreduce.scope_digest_hits",
        scope_digest_hits or _default_scope_digest_hits,
    )

    return chat_service._prepare_context(
        user_id=1,
        organization_id=None,
        question="summarize what we covered",
        history=[],
        settings=ChatSettings(),
        file_uuids=file_uuids,
        speakers=None,
        search_mode="hybrid",
        llm=None,
        rewrite_enabled=False,
    )


# --------------------------------------------------------- the decoupling itself


def test_the_scope_map_runs_even_when_the_ranked_digest_leg_returned_nothing(monkeypatch):
    """THE red case named in the review brief: `result.digests == []` (the
    ranked leg found nothing / was never asked), but the scope is bounded and
    the router wants the digest tier. The overview must still cover every file
    the map returned — proving the map no longer depends on `result.digests`."""
    file_uuids = ["uuid-a", "uuid-b", "uuid-c"]
    map_hits = DigestScopeHits(
        [_digest_hit(f"uuid-{c}", i, f"Recording {c.upper()}") for i, c in enumerate("abc")],
        {"files_without_artifacts": 0},
    )
    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch, file_uuids=file_uuids, map_hits=map_hits, digests=()
    )

    assert overview is not None
    assert overview.block != ""
    for c in "abc":
        assert f"Recording {c.upper()}" in overview.block
    assert meta["map_leg"] == "scope_map"
    assert meta["map_source"] == "code"
    assert "digests_retrieved" not in meta, "the ranked leg found nothing this turn"


def test_unbounded_scope_keeps_the_ranked_leg_behaviour(monkeypatch):
    """`file_uuids is None` (all accessible) cannot be mapped — there is no
    enumerated list to map over — so it must still use the ranked leg exactly
    as before the decoupling."""
    digest_hits = [_digest_hit("uuid-a", 1, "Ranked A"), _digest_hit("uuid-b", 2, "Ranked B")]

    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch,
        file_uuids=None,
        map_hits=DigestScopeHits([], {"files_without_artifacts": 0}),
        digests=digest_hits,
    )

    assert overview is not None
    assert "Ranked A" in overview.block
    assert "Ranked B" in overview.block
    assert meta["map_leg"] == "ranked_digests"
    assert meta["digests_retrieved"] == 2


def test_a_bounded_map_with_nothing_covered_falls_back_to_the_ranked_leg(monkeypatch):
    """The map found nothing to cover (every file in scope lacks a digest row)
    but the ranked leg still surfaced material — degrade to it rather than
    reporting no overview at all."""
    digest_hits = [_digest_hit("uuid-a", 1, "Ranked Only")]

    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch,
        file_uuids=["uuid-a"],
        map_hits=DigestScopeHits([], {"files_without_artifacts": 1}),
        digests=digest_hits,
    )

    assert overview is not None
    assert "Ranked Only" in overview.block
    assert meta["map_leg"] == "ranked_digests"
    assert meta["map_files_without_artifacts"] == 1


def test_nothing_covered_and_no_ranked_digests_produces_no_overview(monkeypatch):
    """Neither leg produced anything: no overview is built at all (not an
    empty one) — `build_overview` already returns `block == ""` for empty
    summaries, and `_prepare_context` must not stamp `meta["overview"]`/
    `meta["map_source"]` in that case."""
    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch,
        file_uuids=["uuid-a"],
        map_hits=DigestScopeHits([], {"files_without_artifacts": 1}),
        digests=(),
    )

    assert overview is None
    assert "overview" not in meta
    assert "map_source" not in meta
    assert "map_leg" not in meta
    assert meta["map_files_without_artifacts"] == 1, "the gap must still be counted"


def _explode(*_a, **_k):
    raise AssertionError("scope_digest_hits must not run for a lookup turn")


def test_a_lookup_turn_never_runs_the_map(monkeypatch):
    """The router did not ask for the digest tier at all (an ordinary lookup):
    the map must not run even though the scope is bounded — `scope_digest_hits`
    is stubbed to explode if called, so this is a red-if-it-runs assertion,
    not merely an absent-key one."""
    _masked, meta, _counted, overview, _synthesis, _recurrence = _prepare(
        monkeypatch,
        file_uuids=["uuid-a"],
        route=Route(intent="lookup", tiers=("chunk",)),
        scope_digest_hits=_explode,
    )

    assert overview is None
    assert "map_leg" not in meta
