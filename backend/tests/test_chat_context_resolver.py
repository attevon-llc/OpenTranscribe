"""Chat scope resolution against a real database (issue #52).

Scope resolution is an AUTHORIZATION boundary: it decides which recordings a
question may be answered from. It deliberately runs in Postgres rather than
trusting the denormalized fields on the OpenSearch document, so these tests use
real rows and real joins — mocking the session here would test the mock.

The property that matters most is the ``None`` vs ``[]`` distinction:
``None`` means "every transcript I can access" and ``[]`` means "nothing
matches". Conflating them would answer questions from the entire library.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import HTTPException

from app.api.deps_context import RequestContext
from app.core import constants as C  # noqa: N812
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.schemas.chat import ChatScope
from app.services.chat.context_resolver import count_scope_files
from app.services.chat.context_resolver import resolve_scope_file_uuids


def _resolved(db, ctx, scope) -> list[str]:
    """resolve_scope_file_uuids narrowed to its list form.

    It returns None only for an empty scope; every caller below passes a
    non-empty one, so this keeps the assertions readable without `assert x is
    not None` noise at each site.
    """
    result = resolve_scope_file_uuids(db, ctx, scope)
    assert result is not None, "expected a resolved list, got None (all-accessible)"
    return result


def _ctx(user, org_id=None) -> RequestContext:
    return RequestContext(user=user, org_id=org_id)


def _make_file(db, user, *, status="completed", quarantined=False, title="Recording"):
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status=status,
        is_quarantined=quarantined,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


# ---------------------------------------------------------------------------
# The None / [] distinction
# ---------------------------------------------------------------------------


def test_empty_scope_resolves_to_none_meaning_all_accessible(db_session, normal_user):
    """None is the 'search everything I can see' signal, enforced downstream."""
    assert resolve_scope_file_uuids(db_session, _ctx(normal_user), ChatScope()) is None


def test_speaker_only_scope_still_resolves_files_to_none(db_session, normal_user):
    """'Everything Dana said, anywhere' keeps the recording scope wide open."""
    scope = ChatScope(speakers=["Dana"])
    assert resolve_scope_file_uuids(db_session, _ctx(normal_user), scope) is None


def test_scope_naming_a_file_you_cannot_see_resolves_to_empty_not_none(
    db_session, normal_user, other_user
):
    """The dangerous confusion: an unauthorized selection must match NOTHING.

    If this returned None, asking about someone else's recording would silently
    search the caller's entire library instead.
    """
    theirs = _make_file(db_session, other_user)
    scope = ChatScope(file_uuids=[str(theirs.uuid)])

    resolved = resolve_scope_file_uuids(db_session, _ctx(normal_user), scope)

    assert resolved == []
    assert resolved is not None


# ---------------------------------------------------------------------------
# Explicit files
# ---------------------------------------------------------------------------


def test_own_completed_file_resolves(db_session, normal_user):
    mine = _make_file(db_session, normal_user)
    scope = ChatScope(file_uuids=[str(mine.uuid)])

    assert resolve_scope_file_uuids(db_session, _ctx(normal_user), scope) == [str(mine.uuid)]


def test_unfinished_file_is_skipped(db_session, normal_user):
    """A file still transcribing has no chunks to retrieve."""
    pending = _make_file(db_session, normal_user, status="processing")
    scope = ChatScope(file_uuids=[str(pending.uuid)])

    assert resolve_scope_file_uuids(db_session, _ctx(normal_user), scope) == []


def test_quarantined_file_is_skipped_for_its_owner(db_session, normal_user):
    """A takedown must remove the recording from chat too, not just the gallery."""
    blocked = _make_file(db_session, normal_user, quarantined=True)
    scope = ChatScope(file_uuids=[str(blocked.uuid)])

    assert resolve_scope_file_uuids(db_session, _ctx(normal_user), scope) == []


def test_unknown_uuid_is_skipped_rather_than_raising(db_session, normal_user):
    scope = ChatScope(file_uuids=[str(uuid_pkg.uuid4())])
    assert resolve_scope_file_uuids(db_session, _ctx(normal_user), scope) == []


def test_inaccessible_files_are_filtered_out_of_a_mixed_selection(
    db_session, normal_user, other_user
):
    mine = _make_file(db_session, normal_user)
    theirs = _make_file(db_session, other_user)
    scope = ChatScope(file_uuids=[str(mine.uuid), str(theirs.uuid)])

    assert resolve_scope_file_uuids(db_session, _ctx(normal_user), scope) == [str(mine.uuid)]


# ---------------------------------------------------------------------------
# Collections and tags
# ---------------------------------------------------------------------------


def test_collection_expands_to_its_completed_members(db_session, normal_user):
    first = _make_file(db_session, normal_user, title="One")
    second = _make_file(db_session, normal_user, title="Two")
    unfinished = _make_file(db_session, normal_user, status="processing", title="Three")

    collection = Collection(uuid=uuid_pkg.uuid4(), name="Q3", user_id=normal_user.id)
    db_session.add(collection)
    db_session.commit()
    for media in (first, second, unfinished):
        db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media.id))
    db_session.commit()

    resolved = _resolved(
        db_session, _ctx(normal_user), ChatScope(collection_uuids=[str(collection.uuid)])
    )

    assert set(resolved) == {str(first.uuid), str(second.uuid)}


def test_another_users_collection_resolves_to_nothing(db_session, normal_user, other_user):
    theirs = _make_file(db_session, other_user)
    collection = Collection(uuid=uuid_pkg.uuid4(), name="Theirs", user_id=other_user.id)
    db_session.add(collection)
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=theirs.id))
    db_session.commit()

    resolved = _resolved(
        db_session, _ctx(normal_user), ChatScope(collection_uuids=[str(collection.uuid)])
    )

    assert resolved == []


def test_tag_expands_to_the_callers_own_tagged_files(db_session, normal_user):
    tagged = _make_file(db_session, normal_user, title="Tagged")
    _make_file(db_session, normal_user, title="Untagged")

    tag = Tag(uuid=uuid_pkg.uuid4(), name=f"topic-{uuid_pkg.uuid4().hex[:6]}")
    db_session.add(tag)
    db_session.commit()
    db_session.add(FileTag(media_file_id=tagged.id, tag_id=tag.id))
    db_session.commit()

    resolved = _resolved(db_session, _ctx(normal_user), ChatScope(tag_names=[tag.name]))

    assert resolved == [str(tagged.uuid)]


def test_tag_does_not_reach_another_users_files(db_session, normal_user, other_user):
    theirs = _make_file(db_session, other_user)
    tag = Tag(uuid=uuid_pkg.uuid4(), name=f"shared-{uuid_pkg.uuid4().hex[:6]}")
    db_session.add(tag)
    db_session.commit()
    db_session.add(FileTag(media_file_id=theirs.id, tag_id=tag.id))
    db_session.commit()

    resolved = _resolved(db_session, _ctx(normal_user), ChatScope(tag_names=[tag.name]))

    assert resolved == []


def test_axes_are_unioned_and_deduplicated(db_session, normal_user):
    """A file selected twice (directly and via a collection) appears once."""
    both = _make_file(db_session, normal_user, title="Both")
    only_collection = _make_file(db_session, normal_user, title="Collection only")

    collection = Collection(uuid=uuid_pkg.uuid4(), name="Mixed", user_id=normal_user.id)
    db_session.add(collection)
    db_session.commit()
    for media in (both, only_collection):
        db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media.id))
    db_session.commit()

    resolved = _resolved(
        db_session,
        _ctx(normal_user),
        ChatScope(file_uuids=[str(both.uuid)], collection_uuids=[str(collection.uuid)]),
    )

    assert sorted(resolved) == sorted({str(both.uuid), str(only_collection.uuid)})
    assert len(resolved) == len(set(resolved))


def test_resolution_is_sorted_so_the_cache_key_is_stable(db_session, normal_user):
    """scope_hash sorts too, but a stable order keeps logs and tests readable."""
    files = [_make_file(db_session, normal_user, title=f"R{i}") for i in range(4)]
    scope = ChatScope(file_uuids=[str(f.uuid) for f in reversed(files)])

    resolved = _resolved(db_session, _ctx(normal_user), scope)

    assert resolved == sorted(resolved)


# ---------------------------------------------------------------------------
# The 500-file ceiling
# ---------------------------------------------------------------------------


def test_oversized_scope_is_rejected_with_400(db_session, normal_user, monkeypatch):
    """Rejected loudly rather than silently truncated — the user chose those files."""
    monkeypatch.setattr(C, "CHAT_MAX_SCOPE_FILES", 2)

    files = [_make_file(db_session, normal_user, title=f"R{i}") for i in range(3)]
    scope = ChatScope(file_uuids=[str(f.uuid) for f in files])

    with pytest.raises(HTTPException) as exc:
        resolve_scope_file_uuids(db_session, _ctx(normal_user), scope)

    assert exc.value.status_code == 400
    assert "maximum" in str(exc.value.detail).lower()


# ---------------------------------------------------------------------------
# count_scope_files (the context-size estimator)
# ---------------------------------------------------------------------------


def test_empty_scope_count_excludes_other_users_files(db_session, normal_user, other_user):
    """Regression: this once counted EVERY user's recordings.

    Content was never exposed — retrieval is gated separately — but the number
    itself disclosed how much other people had uploaded.
    """
    _make_file(db_session, normal_user, title="Mine")
    _make_file(db_session, other_user, title="Theirs A")
    _make_file(db_session, other_user, title="Theirs B")

    assert count_scope_files(db_session, _ctx(normal_user), ChatScope()) == 1


def test_count_reports_past_the_cap_instead_of_raising(db_session, normal_user, monkeypatch):
    """The estimator exists to WARN about oversized selections.

    Raising here would silence the warning at exactly the size it is for.
    """
    monkeypatch.setattr(C, "CHAT_MAX_SCOPE_FILES", 2)

    files = [_make_file(db_session, normal_user, title=f"R{i}") for i in range(3)]
    scope = ChatScope(file_uuids=[str(f.uuid) for f in files])

    assert count_scope_files(db_session, _ctx(normal_user), scope) > 2


def test_count_of_a_scoped_selection_matches_resolution(db_session, normal_user):
    files = [_make_file(db_session, normal_user, title=f"R{i}") for i in range(2)]
    scope = ChatScope(file_uuids=[str(f.uuid) for f in files])

    assert count_scope_files(db_session, _ctx(normal_user), scope) == 2
