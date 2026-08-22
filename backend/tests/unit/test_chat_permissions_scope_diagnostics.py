"""Finding #4: a picker that offers files the scope then silently discards.

`list_media_files` ignores `ownership` for admins and returns every file in the
tenant, but `_resolve_explicit_files` resolves scope as an ordinary user and
silently skips whatever it cannot reach (`logger.info` only) — invisible
outside a log tail. An admin who picks 40 recordings and gets an answer
covering 3 sees no signal that 37 were excluded; `meta["files_searched"]` is
just the post-drop length, which reads as "the scope was always 3".

`resolve_scope_file_uuids`'s new `diagnostics` out-param (mirroring
`search.chunk_retrieval.retrieve_chunks`'s existing one) surfaces the count so
`service.py` can stamp it onto `msg_metadata` as `scope_files_dropped`.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.api.deps_context import RequestContext
from app.models.media import MediaFile
from app.schemas.chat import ChatScope
from app.services.chat.context_resolver import resolve_scope_file_uuids

pytestmark = pytest.mark.unit


def _ctx(user, org_id=None) -> RequestContext:
    return RequestContext(user=user, org_id=org_id)


def _make_file(db, user, *, title="Recording"):
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def test_diagnostics_reports_how_many_explicit_picks_were_dropped(
    db_session, admin_user, other_user
):
    """LEAK-shaped-but-not-a-leak: the admin picks a file they have no real
    access to (a file another user owns and never shared). Scope correctly
    resolves it to nothing, and the caller must be told ONE of the picks was
    dropped rather than seeing a silently smaller scope."""
    mine = _make_file(db_session, admin_user, title="Admin's own")
    theirs = _make_file(db_session, other_user, title="Unrelated")

    diagnostics: dict = {}
    resolved = resolve_scope_file_uuids(
        db_session,
        _ctx(admin_user),
        ChatScope(file_uuids=[str(mine.uuid), str(theirs.uuid)]),
        diagnostics=diagnostics,
    )

    assert resolved == [str(mine.uuid)]
    assert diagnostics["files_dropped"] == 1


def test_diagnostics_key_absent_when_nothing_was_dropped(db_session, normal_user):
    """Control: a scope that resolves everything requested must not carry the
    key at all — an absent key, not a zero, is what `service.py` checks."""
    mine = _make_file(db_session, normal_user, title="Mine")

    diagnostics: dict = {}
    resolved = resolve_scope_file_uuids(
        db_session,
        _ctx(normal_user),
        ChatScope(file_uuids=[str(mine.uuid)]),
        diagnostics=diagnostics,
    )

    assert resolved == [str(mine.uuid)]
    assert "files_dropped" not in diagnostics


def test_diagnostics_ignores_drops_on_the_collection_and_tag_axes(db_session, admin_user):
    """`files_dropped` is scoped to the EXPLICIT-file axis only — the one a
    file picker UI actually offers. A collection/tag scope that resolves to
    nothing is a different, already-explained outcome (an empty scope), not a
    silent drop from an explicit pick list."""
    diagnostics: dict = {}
    resolved = resolve_scope_file_uuids(
        db_session,
        _ctx(admin_user),
        ChatScope(collection_uuids=[str(uuid_pkg.uuid4())]),
        diagnostics=diagnostics,
    )

    assert resolved == []
    assert "files_dropped" not in diagnostics


def test_diagnostics_out_param_is_optional(db_session, normal_user):
    """The default (`diagnostics=None`) must not raise — every existing caller
    that predates this parameter calls the function with no keyword at all."""
    mine = _make_file(db_session, normal_user, title="Mine")

    resolved = resolve_scope_file_uuids(
        db_session, _ctx(normal_user), ChatScope(file_uuids=[str(mine.uuid)])
    )

    assert resolved == [str(mine.uuid)]
