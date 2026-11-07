"""Chat leaves the caller's hidden files out of a WIDE scope only (issue #1102).

"All transcripts" and a tag scope are nets the caller did not draw file by file, so their hidden
files stay out, exactly as they stay out of the gallery. A file the caller picked (or a
collection they picked) is kept: hide is a view preference, not access control.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

from app.models.media import MediaFile
from app.models.tag import FileTag
from app.models.tag import Tag
from app.schemas.chat import ChatScope
from app.services import hidden_media_service
from app.services.chat import context_prep
from app.services.chat.context_resolver import count_scope_files
from app.services.chat.context_resolver import resolve_scope_file_uuids
from app.services.chat.context_resolver import unscoped_hidden_file_uuids
from app.services.chat.redactor import MaskedChunk
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.settings import ChatSettings
from app.services.request_context import RequestContext
from app.services.search.chunk_retrieval import ChunkHit


def _file(db_session, owner) -> MediaFile:
    row = MediaFile(
        uuid=uuid.uuid4(),
        user_id=owner.id,
        filename="Tidewell sync.mp4",
        title="Tidewell sync",
        storage_path=f"media/test/{uuid.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _hide(db_session, user, media_file) -> None:
    hidden_media_service.hide(db_session, user.id, [media_file.id])
    db_session.commit()


def _ctx(user) -> RequestContext:
    return RequestContext(user=user, org_id=None)


# --- scope resolution -------------------------------------------------------------------------


def test_an_explicitly_picked_hidden_file_stays_in_scope(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    _hide(db_session, normal_user, media_file)

    resolved = resolve_scope_file_uuids(
        db_session, _ctx(normal_user), ChatScope(file_uuids=[str(media_file.uuid)])
    )

    assert resolved == [str(media_file.uuid)]


def test_a_tag_scope_leaves_the_callers_hidden_files_out(db_session, normal_user):
    hidden = _file(db_session, normal_user)
    shown = _file(db_session, normal_user)
    tag = Tag(uuid=uuid.uuid4(), name=f"tide-{uuid.uuid4().hex[:6]}", user_id=normal_user.id)
    db_session.add(tag)
    db_session.commit()
    db_session.add_all([FileTag(media_file_id=f.id, tag_id=tag.id) for f in (hidden, shown)])
    db_session.commit()
    scope = ChatScope(tag_names=[tag.name])
    assert len(resolve_scope_file_uuids(db_session, _ctx(normal_user), scope) or []) == 2

    _hide(db_session, normal_user, hidden)

    assert resolve_scope_file_uuids(db_session, _ctx(normal_user), scope) == [str(shown.uuid)]


def test_all_transcripts_counts_and_resolves_without_the_hidden_files(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    before = count_scope_files(db_session, _ctx(normal_user), ChatScope())
    assert unscoped_hidden_file_uuids(db_session, _ctx(normal_user), None) == frozenset()

    _hide(db_session, normal_user, media_file)

    assert count_scope_files(db_session, _ctx(normal_user), ChatScope()) == before - 1
    assert unscoped_hidden_file_uuids(db_session, _ctx(normal_user), None) == {str(media_file.uuid)}
    picked = unscoped_hidden_file_uuids(db_session, _ctx(normal_user), [str(media_file.uuid)])
    assert picked == frozenset(), "a chosen scope keeps hidden files"


def test_another_users_unscoped_turn_keeps_my_hidden_file(db_session, normal_user, other_user):
    media_file = _file(db_session, normal_user)
    _hide(db_session, normal_user, media_file)
    assert unscoped_hidden_file_uuids(db_session, _ctx(other_user), None) == frozenset()


# --- phase 3.5 of a turn ----------------------------------------------------------------------


def _hit(file_uuid: str, index: int) -> ChunkHit:
    return ChunkHit(file_uuid=file_uuid, file_id=index, chunk_index=index, content=f"c{index}")


def _run_turn(monkeypatch, db_session, *, file_uuids, hidden, hits):
    monkeypatch.setattr(
        context_prep,
        "retrieve_context",
        lambda **_kwargs: RetrievalResult(chunks=list(hits), retrieved=len(hits)),
    )
    monkeypatch.setattr(
        context_prep,
        "mask_chunks",
        lambda _factory, chunks, _user_id, **_kw: [
            MaskedChunk(source=h, content=h.content) for h in chunks
        ],
    )

    @contextmanager
    def _fixture_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _fixture_session)
    masked, meta, *_rest = context_prep._prepare_context(
        user_id=1,
        organization_id=None,
        question="What did they decide?",
        history=[],
        settings=ChatSettings(),
        file_uuids=file_uuids,
        speakers=None,
        search_mode="hybrid",
        llm=None,
        rewrite_enabled=False,
        hidden_file_uuids=hidden,
    )
    return [m.source.file_uuid for m in masked], meta


def test_an_unscoped_turn_drops_the_hidden_files_evidence(monkeypatch, db_session, normal_user):
    hidden = str(_file(db_session, normal_user).uuid)
    shown = str(_file(db_session, normal_user).uuid)
    hits = [_hit(hidden, 1), _hit(shown, 2)]

    kept, meta = _run_turn(
        monkeypatch, db_session, file_uuids=None, hidden=frozenset({hidden}), hits=hits
    )

    assert kept == [shown]
    assert meta["chunks_dropped_hidden"] == 1
    assert "chunks_dropped_quarantined" not in meta, "a hide is never counted as a takedown"


def test_a_scoped_turn_keeps_a_hidden_file_it_names(monkeypatch, db_session, normal_user):
    hidden = str(_file(db_session, normal_user).uuid)
    shown = str(_file(db_session, normal_user).uuid)
    hits = [_hit(hidden, 1), _hit(shown, 2)]

    kept, meta = _run_turn(
        monkeypatch,
        db_session,
        file_uuids=[hidden, shown],
        hidden=frozenset({hidden}),
        hits=hits,
    )

    assert sorted(kept) == sorted([hidden, shown])
    assert "chunks_dropped_hidden" not in meta
