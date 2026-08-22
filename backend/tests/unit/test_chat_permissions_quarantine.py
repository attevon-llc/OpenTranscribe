"""The chat-plane quarantine gate (W2.0g fix #1).

Quarantine is not an OpenSearch filter field — `api/endpoints/search.py`'s
`_drop_quarantined_search_hits` post-filters the search UI's results in
Postgres for exactly that reason. Chat had no equivalent: an explicitly-scoped
turn is protected by `context_resolver._visible_files_query` (which excludes
`is_quarantined` files for EVERY caller, admin included — the admin bypass this
docstring used to describe was itself removed in the pass that added the tests
below, see `test_scope_resolution_has_no_admin_bypass_on_any_axis`), but an
**unscoped** turn (`file_uuids=None`, `scope.is_empty`) never reaches that
resolver at all — `resolve_scope_file_uuids` returns `None` and retrieval goes
straight to OpenSearch, which still has the quarantined file's chunks and
digest sections indexed.

`service._drop_quarantined_hits` closes that gap in `_prepare_context` phase
3.5, applied to BOTH the chunk and digest hit lists. These tests cover it at
two levels: the helper directly (real Postgres, no service plumbing), and the
full `_prepare_context` unscoped path with retrieval mocked to hand back a
quarantined hit mixed with an ordinary one — the permission test matrix's row
T1, both halves: the LEAK (quarantined content must not reach masking) and its
SHARED-VISIBILITY control (an ordinary shared/accessible hit must not be
collaterally dropped).

⚠️ **This file used to be a complete-looking fix that was not.** It filtered
`result.chunks` and `result.digests` (the ranked tiers) and stopped there. The
counted/aggregation tier (`aggregation_service.answer_aggregation`) runs a
phase EARLIER and was never routed through `_drop_quarantined_hits` at all —
see `tests/unit/test_chat_permissions_aggregation_quarantine.py` for that gap
and its close. `test_scope_resolution_has_no_admin_bypass_on_any_axis` and
`test_scope_digest_hits_excludes_a_quarantined_file` below cover two more
paths this file's original pass did not: the map-reduce overview tier
(`mapreduce.scope_digest_hits`, Finding #2) and the agreement between the four
scope-resolution paths — explicit files, collections, tags, and the phase-3.5
hits drop — for an ADMIN caller specifically (Finding #3). The chosen policy is
**no admin bypass on quarantine anywhere in chat**: chat is a retrieval
surface, not the admin review one (`GET /admin/files/quarantined`), and phase
3.5 already dropped a quarantined file's hits unconditionally — three axes
that could still admit one into scope while the fourth always removed it was
the "two access rules disagreeing" shape this module exists to close.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager

import pytest

from app.services.chat import service as chat_service
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.settings import ChatSettings
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


def _make_file(db, user, *, quarantined=False, title="Recording"):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        is_quarantined=quarantined,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def _hit(file_uuid: str, *, index: int = 0, digest_section: int | None = None) -> ChunkHit:
    return ChunkHit(
        file_uuid=file_uuid,
        file_id=index,
        chunk_index=index,
        content=f"content {index}",
        title="Recording",
        digest_section=digest_section,
    )


# ---------------------------------------------------------------------------
# `_drop_quarantined_hits` directly — real Postgres, no service plumbing.
# ---------------------------------------------------------------------------


def test_drop_quarantined_hits_removes_a_quarantined_files_chunk(db_session, normal_user):
    """LEAK: a quarantined file's chunk hit must not survive the filter."""
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")

    kept = chat_service._drop_quarantined_hits(db_session, [_hit(str(blocked.uuid))])

    assert kept == []


def test_drop_quarantined_hits_removes_a_quarantined_files_digest(db_session, normal_user):
    """LEAK, digest plane: `mask_chunks`/`mask_digests` are two different callers,
    and the earlier version of this fix only ran the check on one of them."""
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")

    kept = chat_service._drop_quarantined_hits(
        db_session, [_hit(str(blocked.uuid), digest_section=0)]
    )

    assert kept == []


def test_drop_quarantined_hits_keeps_an_ordinary_accessible_hit(db_session, normal_user):
    """SHARED-VISIBILITY control: an accessible, non-quarantined hit is untouched.

    A leak fix that also drops ordinary content is a failure, not a pass — this
    repo has shipped incidents in both directions.
    """
    ok = _make_file(db_session, normal_user, quarantined=False, title="Fine")

    kept = chat_service._drop_quarantined_hits(db_session, [_hit(str(ok.uuid))])

    assert [h.file_uuid for h in kept] == [str(ok.uuid)]


def test_drop_quarantined_hits_filters_a_mixed_list(db_session, normal_user):
    """One quarantined hit must not take down the rest of the batch with it."""
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")
    ok = _make_file(db_session, normal_user, quarantined=False, title="Fine")

    kept = chat_service._drop_quarantined_hits(
        db_session, [_hit(str(blocked.uuid), index=0), _hit(str(ok.uuid), index=1)]
    )

    assert [h.file_uuid for h in kept] == [str(ok.uuid)]


def test_drop_quarantined_hits_handles_an_empty_list(db_session):
    """No unconditional query against an empty batch."""
    assert chat_service._drop_quarantined_hits(db_session, []) == []


# ---------------------------------------------------------------------------
# `_prepare_context`, unscoped (`file_uuids=None` — `scope.is_empty` upstream).
# ---------------------------------------------------------------------------


@contextmanager
def _test_session_scope(db_session):
    yield db_session
    db_session.commit()


class _FakeLLMConfig:
    """Duck-typed for `redaction.llm_guard.is_local_provider` (reads `.provider`/
    `.base_url` only). A remote provider — the masking-locality decision is
    orthogonal to the quarantine gate under test here, so any fixed answer does."""

    provider = "openai"
    base_url = None


class _FakeLLM:
    config = _FakeLLMConfig()


def _run_prepare_context(monkeypatch, db_session, user, *, chunks, digests):
    """Drive the real, synchronous `_prepare_context` with retrieval mocked and
    the counted tier forced off, so masking is the only real Postgres work left
    beside the quarantine drop under test.
    """

    def _scope():
        return _test_session_scope(db_session)

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)

    def _retrieve(**kwargs):
        return RetrievalResult(chunks=list(chunks), digests=list(digests), retrieved=len(chunks))

    monkeypatch.setattr(chat_service, "retrieve_context", _retrieve)
    # Force the counted tier off regardless of how the router classifies the
    # question — this test is about the quarantine gate, not aggregation.
    monkeypatch.setattr(
        "app.services.chat.aggregation_service.answer_aggregation", lambda *a, **k: None
    )
    # Masking is exercised elsewhere (test_chat_redactor.py); here it must run
    # only over what SURVIVES the quarantine drop, so make it an identity
    # pass-through and assert on what it was handed.
    from app.services.chat.redactor import MaskedChunk

    monkeypatch.setattr(
        chat_service,
        "mask_chunks",
        lambda _factory, chunks, _user_id, **_kwargs: [
            MaskedChunk(source=c, content=c.content) for c in chunks
        ],
    )

    return chat_service._prepare_context(
        user_id=user.id,
        organization_id=None,
        question="What did Dana say about the roadmap?",
        history=[],
        settings=ChatSettings(),
        file_uuids=None,  # scope.is_empty upstream — the unscoped path
        speakers=None,
        search_mode="hybrid",
        llm=_FakeLLM(),
        rewrite_enabled=False,
    )


def test_unscoped_turn_retrieves_zero_chunk_hits_from_a_quarantined_file(
    monkeypatch, db_session, normal_user
):
    """The permission matrix's row T1, LEAK half.

    An unscoped chat turn (no explicit files/collections/tags — `resolve_scope_
    file_uuids` returns `None`) never routes through `context_resolver`'s
    Postgres quarantine filter at all. Before this fix nothing else in the chat
    pipeline enforced it either, so a quarantined recording's chunks were fully
    retrievable through an ordinary "ask about anything" turn.
    """
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")

    masked, _meta, _counted, _overview, _synthesis, _recurrence = _run_prepare_context(
        monkeypatch, db_session, normal_user, chunks=[_hit(str(blocked.uuid))], digests=[]
    )

    assert masked == []


def test_unscoped_turn_retrieves_zero_digest_hits_from_a_quarantined_file(
    monkeypatch, db_session, normal_user
):
    """The permission matrix's row T1, LEAK half — digest plane.

    `mask_digests` is a SEPARATE call from `mask_chunks` (issue: two maskers,
    not interchangeable), so the drop has to run over `result.digests` too, not
    just `result.chunks`.
    """
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")

    masked, meta, _counted, _overview, _synthesis, _recurrence = _run_prepare_context(
        monkeypatch,
        db_session,
        normal_user,
        chunks=[],
        digests=[_hit(str(blocked.uuid), digest_section=0)],
    )

    assert masked == []
    assert meta.get("digests_retrieved", 0) == 0


def test_quarantine_drop_does_not_impersonate_a_masking_failure(
    monkeypatch, db_session, normal_user
):
    """Finding #5: a turn whose only hits were quarantined must not emit the
    same diagnostics as a masking failure. Before the fix, `retrieved` still
    counted the quarantined chunk (never decremented at phase 3.5), so
    `no_context` fired with `retrieved=1` beside `chunks_dropped_empty_after_
    masking == 0` — two facts that contradicted each other and pointed the
    reader at masking, which never ran on this chunk at all.
    """
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")

    _masked, meta, _counted, _overview, _synthesis, _recurrence = _run_prepare_context(
        monkeypatch, db_session, normal_user, chunks=[_hit(str(blocked.uuid))], digests=[]
    )

    assert meta["chunks_dropped_quarantined"] == 1
    # The quarantine drop is now reflected in `retrieved`, so a reader sees an
    # ordinary "nothing retrieved" — not a phantom masking failure.
    assert meta["retrieved"] == 0
    assert meta["chunks_dropped_empty_after_masking"] == 0


def test_quarantine_drop_counter_absent_when_nothing_was_quarantined(
    monkeypatch, db_session, normal_user
):
    """Control: an ordinary turn must not carry the new key at all — it is
    documented (`service.py`) as present only when a drop actually happened."""
    ok = _make_file(db_session, normal_user, quarantined=False, title="Fine")

    _masked, meta, _counted, _overview, _synthesis, _recurrence = _run_prepare_context(
        monkeypatch, db_session, normal_user, chunks=[_hit(str(ok.uuid))], digests=[]
    )

    assert "chunks_dropped_quarantined" not in meta


def test_unscoped_turn_still_retrieves_an_ordinary_accessible_file(
    monkeypatch, db_session, normal_user
):
    """The permission matrix's row T1, SHARED-VISIBILITY half.

    A file the caller can genuinely access — not quarantined — must survive
    the unscoped path unchanged. A quarantine fix that also drops ordinary
    content is a failure, not a pass.
    """
    ok = _make_file(db_session, normal_user, quarantined=False, title="Fine")

    masked, _meta, _counted, _overview, _synthesis, _recurrence = _run_prepare_context(
        monkeypatch, db_session, normal_user, chunks=[_hit(str(ok.uuid))], digests=[]
    )

    assert len(masked) == 1
    assert masked[0].source.file_uuid == str(ok.uuid)


# ---------------------------------------------------------------------------
# Finding #2 — `mapreduce.scope_digest_hits` (the MAP leg) must agree with the
# phase-3.5 drop on the RANKED digest leg, for the same bounded scope.
# ---------------------------------------------------------------------------


def test_scope_digest_hits_excludes_a_quarantined_file(db_session, normal_user):
    """LEAK: the map step must not serve a taken-down file's digest sections,
    even though `file_uuids` is trusted scope by contract — this filter is
    defense in depth against a caller resolving scope for a different
    permission profile than the one that ends up reading the map."""
    from app.services.chat.mapreduce import scope_digest_hits

    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")

    hits = scope_digest_hits(db_session, [str(blocked.uuid)])

    assert list(hits) == []


def test_scope_digest_hits_still_covers_an_ordinary_accessible_file(db_session, normal_user):
    """SHARED-VISIBILITY control."""
    from app.models.file_facts import FileFacts
    from app.services.chat.mapreduce import scope_digest_hits

    ok = _make_file(db_session, normal_user, quarantined=False, title="Fine")
    db_session.add(
        FileFacts(
            media_file_id=ok.id,
            generator_version="1.1.1",
            source_fingerprint="deadbeef",
            facts={},
            digest={"sections": [{"index": 0, "text": "a section", "start_time": 0.0}]},
            keyphrases={},
        )
    )
    db_session.commit()

    hits = scope_digest_hits(db_session, [str(ok.uuid)])

    assert [h.file_uuid for h in hits] == [str(ok.uuid)]


# ---------------------------------------------------------------------------
# Finding #3 — the four scope-resolution paths agree for an ADMIN caller: no
# axis (explicit files / collections / tags / phase-3.5 hits) admits a
# quarantined file into a chat turn, regardless of who is asking.
# ---------------------------------------------------------------------------


def test_scope_resolution_has_no_admin_bypass_on_any_axis(db_session, admin_user):
    """The policy this module now enforces, pinned directly: an admin's
    EXPLICIT file pick, COLLECTION pick, and TAG pick all resolve a quarantined
    file to nothing — agreeing with phase 3.5's unconditional drop, which
    `test_unscoped_turn_retrieves_zero_chunk_hits_from_a_quarantined_file`
    above already covers for the unscoped case."""
    import uuid as uuid_pkg

    from app.api.deps_context import RequestContext
    from app.models.media import Collection
    from app.models.media import CollectionMember
    from app.models.media import FileTag
    from app.models.media import Tag
    from app.schemas.chat import ChatScope
    from app.services.chat.context_resolver import resolve_scope_file_uuids

    ctx = RequestContext(user=admin_user, org_id=None)
    blocked = _make_file(db_session, admin_user, quarantined=True, title="Blocked")

    # Explicit files axis.
    explicit_scope = ChatScope(file_uuids=[str(blocked.uuid)])
    assert resolve_scope_file_uuids(db_session, ctx, explicit_scope) == []

    # Collections axis: the admin owns the collection, so ordinary collection
    # access is not the variable under test — only quarantine is.
    collection = Collection(uuid=uuid_pkg.uuid4(), name="Admin's own", user_id=admin_user.id)
    db_session.add(collection)
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=blocked.id))
    db_session.commit()
    collection_scope = ChatScope(collection_uuids=[str(collection.uuid)])
    assert resolve_scope_file_uuids(db_session, ctx, collection_scope) == []

    # Tags axis: same, owned tag.
    tag = Tag(name="atlas-admin-quarantine-probe", user_id=admin_user.id)
    db_session.add(tag)
    db_session.commit()
    db_session.add(FileTag(media_file_id=blocked.id, tag_id=tag.id))
    db_session.commit()
    tag_scope = ChatScope(tag_names=[tag.name])
    assert resolve_scope_file_uuids(db_session, ctx, tag_scope) == []


def test_count_scope_files_matches_retrieval_for_an_admin_with_a_quarantined_file(
    db_session, admin_user
):
    """Finding #6: `count_scope_files`'s "All transcripts" estimate must not
    over-count relative to what retrieval will actually search. Before the
    fix, this counted through `_visible_files_query`'s admin-bypassing branch
    while phase 3.5 dropped the same file unconditionally — the estimate said
    2 while the turn would search 1."""
    from app.api.deps_context import RequestContext
    from app.schemas.chat import ChatScope
    from app.services.chat.context_resolver import count_scope_files

    ctx = RequestContext(user=admin_user, org_id=None)
    _make_file(db_session, admin_user, quarantined=False, title="Fine")
    _make_file(db_session, admin_user, quarantined=True, title="Blocked")

    count = count_scope_files(db_session, ctx, ChatScope())

    assert count == 1


def test_scope_resolution_admin_bypass_removal_does_not_block_real_admin_access(
    db_session, admin_user
):
    """SHARED-VISIBILITY control for Finding #3: removing the admin bypass on
    quarantine must not also block an admin's ordinary, non-quarantined,
    genuinely-owned recording."""
    from app.api.deps_context import RequestContext
    from app.schemas.chat import ChatScope
    from app.services.chat.context_resolver import resolve_scope_file_uuids

    ctx = RequestContext(user=admin_user, org_id=None)
    ok = _make_file(db_session, admin_user, quarantined=False, title="Fine")

    resolved = resolve_scope_file_uuids(db_session, ctx, ChatScope(file_uuids=[str(ok.uuid)]))

    assert resolved == [str(ok.uuid)]
