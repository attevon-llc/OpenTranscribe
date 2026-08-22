"""Two more scope-resolution permission fixes (W2.0g), alongside the
pre-existing coverage in ``test_chat_context_resolver.py``:

* Fix #3 — the admin bypass (``is_admin=True``) in ``_resolve_explicit_files``
  and ``_resolve_collections`` used to let a file/collection the admin had
  neither ownership nor a real share of resolve into scope. Retrieval's
  ``accessible_user_ids`` OpenSearch filter has no matching admin arm, so that
  file would then retrieve ZERO excerpts — a silent, unexplained empty answer,
  the exact shape of a leak (two access rules disagreeing) without being one.
  These tests prove: for an admin, everything ``resolve_scope_file_uuids``
  returns is a subset of what ``get_accessible_file_ids_subquery`` — the same
  rule retrieval enforces — would call reachable (the LEAK-shaped half), and
  that an admin with a REAL share still resolves normally (the
  SHARED-VISIBILITY half — the fix must not also break genuine admin access).

* Fix #5 — ``count_scope_files``' empty-scope branch used to count only the
  caller's OWNED files for "All transcripts", so a caller whose library is
  entirely shared-with-them saw the context-size estimator report 0
  recordings / ~0% of context for a scope that would, in fact, search
  everything they can read. It now counts through the same accessible-files
  subquery every other axis in this module uses.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.api.deps_context import RequestContext
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.sharing import CollectionShare
from app.schemas.chat import ChatScope
from app.services.chat.context_resolver import count_scope_files
from app.services.chat.context_resolver import resolve_scope_file_uuids
from app.services.permission_service import PermissionService


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


def _share_collection(db, collection, *, owner, with_user):
    db.add(
        CollectionShare(
            uuid=uuid_pkg.uuid4(),
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=with_user.id,
            permission="viewer",
        )
    )
    db.commit()


def _collection_with(db, owner, media, *, name="Shared"):
    collection = Collection(uuid=uuid_pkg.uuid4(), name=name, user_id=owner.id)
    db.add(collection)
    db.commit()
    db.add(CollectionMember(collection_id=collection.id, media_file_id=media.id))
    db.commit()
    return collection


def _accessible_uuids(db, user, org_id=None) -> set[str]:
    """What retrieval could actually reach for ``user`` — the ground truth
    scope resolution must never exceed.

    ``organization_id`` is threaded through explicitly rather than left at the
    ``get_accessible_file_ids_subquery`` default: the default is the
    ``UNSCOPED`` sentinel (no tenant gate at all, not "personal scope"), which
    is exactly the shape ``test_chat_permission_gate_contract.py`` exists to
    forbid in the package this test's own subject module lives beside. Every
    caller here uses the personal scope (``_ctx(user)`` defaults ``org_id`` to
    ``None``), so this must match.
    """
    subq = PermissionService.get_accessible_file_ids_subquery(db, user.id, organization_id=org_id)
    rows = db.query(MediaFile.uuid).filter(MediaFile.id.in_(db.query(subq.c.id))).all()
    return {str(row[0]) for row in rows}


# ---------------------------------------------------------------------------
# Fix #3 — admin scope resolution vs. what retrieval can actually serve
# ---------------------------------------------------------------------------


def test_admin_explicit_scope_of_an_unrelated_file_resolves_to_nothing(
    db_session, admin_user, other_user
):
    """LEAK-shaped: an admin naming a file they have no real access to used to
    resolve it into scope anyway (the ``is_admin=True`` bypass), even though
    retrieval could never actually produce an excerpt from it. Resolving with
    no admin bypass makes scope agree with what is retrievable: nothing.
    """
    theirs = _make_file(db_session, other_user, title="Unrelated")
    scope = ChatScope(file_uuids=[str(theirs.uuid)])

    resolved = resolve_scope_file_uuids(db_session, _ctx(admin_user), scope)

    assert resolved == []


def test_admin_explicit_scope_of_a_genuinely_shared_file_still_resolves(
    db_session, admin_user, other_user
):
    """SHARED-VISIBILITY control: dropping the bypass must not block an admin
    who has REAL access via an ordinary share."""
    theirs = _make_file(db_session, other_user, title="Shared with admin")
    collection = _collection_with(db_session, other_user, theirs)
    _share_collection(db_session, collection, owner=other_user, with_user=admin_user)
    scope = ChatScope(file_uuids=[str(theirs.uuid)])

    resolved = resolve_scope_file_uuids(db_session, _ctx(admin_user), scope)

    assert resolved == [str(theirs.uuid)]


def test_admin_collection_scope_of_an_unshared_collection_resolves_to_nothing(
    db_session, admin_user, other_user
):
    """Same fix, the collection axis: an admin naming a collection they were
    never given access to must not have it resolve anyway."""
    theirs = _make_file(db_session, other_user, title="Unrelated")
    collection = _collection_with(db_session, other_user, theirs, name="Not admin's")
    scope = ChatScope(collection_uuids=[str(collection.uuid)])

    resolved = resolve_scope_file_uuids(db_session, _ctx(admin_user), scope)

    assert resolved == []


def test_admin_collection_scope_of_a_shared_collection_still_resolves(
    db_session, admin_user, other_user
):
    """SHARED-VISIBILITY control for the collection axis."""
    theirs = _make_file(db_session, other_user, title="Shared collection member")
    collection = _collection_with(db_session, other_user, theirs, name="Shared with admin")
    _share_collection(db_session, collection, owner=other_user, with_user=admin_user)
    scope = ChatScope(collection_uuids=[str(collection.uuid)])

    resolved = resolve_scope_file_uuids(db_session, _ctx(admin_user), scope)

    assert resolved == [str(theirs.uuid)]


def test_admin_resolved_scope_is_always_a_subset_of_what_retrieval_can_reach(
    db_session, admin_user, other_user
):
    """The general property the two tests above are instances of: for an
    admin, chat scope must never resolve wider than the accessible-files
    subquery retrieval's OpenSearch filter is built from."""
    unrelated = _make_file(db_session, other_user, title="Unrelated")
    shared = _make_file(db_session, other_user, title="Shared")
    collection = _collection_with(db_session, other_user, shared, name="Shared with admin")
    _share_collection(db_session, collection, owner=other_user, with_user=admin_user)

    scope = ChatScope(file_uuids=[str(unrelated.uuid), str(shared.uuid)])
    resolved = resolve_scope_file_uuids(db_session, _ctx(admin_user), scope)
    reachable = _accessible_uuids(db_session, admin_user)

    assert resolved is not None
    assert set(resolved) <= reachable
    assert set(resolved) == {str(shared.uuid)}


# ---------------------------------------------------------------------------
# Fix #5 — count_scope_files, the "All transcripts" estimator
# ---------------------------------------------------------------------------


def test_empty_scope_count_includes_files_shared_with_the_caller(
    db_session, normal_user, other_user
):
    """SHARED-VISIBILITY: a caller who owns nothing but has a shared recording
    must see a non-zero estimate for 'All transcripts', because that is what
    the turn will actually search.

    Before the fix this asserted 0 — the estimator counted only OWNED files,
    so a shared-only library silently reported no context at all.
    """
    theirs = _make_file(db_session, other_user, title="Shared with me")
    collection = _collection_with(db_session, other_user, theirs)
    _share_collection(db_session, collection, owner=other_user, with_user=normal_user)

    count = count_scope_files(db_session, _ctx(normal_user), ChatScope())

    assert count == 1


def test_empty_scope_count_still_excludes_an_unshared_file_of_another_user(
    db_session, normal_user, other_user
):
    """LEAK control, restated for the fixed implementation: counting through
    the accessible-files subquery must not regress the #431-era fix this
    module's sibling test already pins for the owned-only shape — an
    unrelated user's private file still must not count."""
    _make_file(db_session, other_user, title="Not shared")

    count = count_scope_files(db_session, _ctx(normal_user), ChatScope())

    assert count == 0


def test_empty_scope_count_unions_owned_and_shared(db_session, normal_user, other_user):
    _make_file(db_session, normal_user, title="Mine")
    theirs = _make_file(db_session, other_user, title="Shared with me")
    collection = _collection_with(db_session, other_user, theirs)
    _share_collection(db_session, collection, owner=other_user, with_user=normal_user)

    count = count_scope_files(db_session, _ctx(normal_user), ChatScope())

    assert count == 2
