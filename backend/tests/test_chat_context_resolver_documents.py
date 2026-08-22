"""Chat scope resolution for DOCUMENT uuids (v400, #362 lane C3-remainder).

``PickerDocumentsTab.svelte`` writes selected document uuids into the same
``ChatScope.file_uuids`` array ``PickerFilesTab.svelte`` uses (see that component's own
docstring). Before this, ``context_resolver._resolve_explicit_files`` only ever tried a
uuid against ``MediaFile`` — a selected document silently resolved to nothing, which is
what made the estimator report "0 recordings" for a document-only selection. These tests
pin the fix: ``_resolve_explicit_document`` is tried when the ``MediaFile`` lookup misses,
respects the same no-admin-bypass / quarantine / completed-only rules the media-file axis
already enforces, and a sharee sees exactly what their ``DocumentShare`` grants — no more,
no less (the T-matrix pair: visible per policy, no cross-user leak).
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.api.deps_context import RequestContext
from app.models.document import Document
from app.models.document import DocumentShare
from app.schemas.chat import ChatScope
from app.services.chat.context_resolver import count_scope_files
from app.services.chat.context_resolver import resolve_scope_file_uuids


def _ctx(user, org_id=None) -> RequestContext:
    return RequestContext(user=user, org_id=org_id)


def _make_document(db, user, *, status="completed", is_quarantined=False, organization_id=None):
    doc = Document(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        organization_id=organization_id,
        filename=f"{uuid_pkg.uuid4().hex[:8]}.pdf",
        storage_path=f"documents/test/{uuid_pkg.uuid4()}.pdf",
        file_size=100,
        content_type="application/pdf",
        status=status,
        is_quarantined=is_quarantined,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _share_document(db, document, *, owner, with_user=None, with_group=None, permission="viewer"):
    db.add(
        DocumentShare(
            uuid=uuid_pkg.uuid4(),
            document_id=document.id,
            shared_by_id=owner.id,
            target_type="user" if with_user else "group",
            target_user_id=with_user.id if with_user else None,
            target_group_id=with_group.id if with_group else None,
            permission=permission,
        )
    )
    db.commit()


def test_an_owned_document_resolves_into_scope(db_session, normal_user):
    doc = _make_document(db_session, normal_user)
    scope = ChatScope(file_uuids=[str(doc.uuid)])

    resolved = resolve_scope_file_uuids(db_session, _ctx(normal_user), scope)

    assert resolved == [str(doc.uuid)]


def test_a_shared_document_resolves_for_the_sharee(db_session, normal_user, other_user):
    doc = _make_document(db_session, normal_user)
    _share_document(db_session, doc, owner=normal_user, with_user=other_user)
    scope = ChatScope(file_uuids=[str(doc.uuid)])

    resolved = resolve_scope_file_uuids(db_session, _ctx(other_user), scope)

    assert resolved == [str(doc.uuid)]


def test_an_unshared_document_does_not_leak_to_another_user(db_session, normal_user, other_user):
    """The T-matrix's other half: no share, no access — for anyone but the owner."""
    doc = _make_document(db_session, normal_user)
    scope = ChatScope(file_uuids=[str(doc.uuid)])

    resolved = resolve_scope_file_uuids(db_session, _ctx(other_user), scope)

    assert resolved == []


def test_admin_with_no_real_access_resolves_to_nothing(db_session, normal_user, admin_user):
    """Same no-admin-bypass rule the media-file axis already enforces
    (``test_chat_permissions_context_resolver.py``'s Fix #3) — an admin naming a
    document they have no ownership or share of must not silently resolve it,
    since retrieval's ``accessible_user_ids`` filter has no admin arm to match.
    """
    doc = _make_document(db_session, normal_user)
    scope = ChatScope(file_uuids=[str(doc.uuid)])

    resolved = resolve_scope_file_uuids(db_session, _ctx(admin_user), scope)

    assert resolved == []


def test_a_quarantined_document_is_excluded_even_for_the_owner(db_session, normal_user):
    doc = _make_document(db_session, normal_user, is_quarantined=True)
    scope = ChatScope(file_uuids=[str(doc.uuid)])

    resolved = resolve_scope_file_uuids(db_session, _ctx(normal_user), scope)

    assert resolved == []


def test_a_not_yet_completed_document_is_excluded(db_session, normal_user):
    doc = _make_document(db_session, normal_user, status="processing")
    scope = ChatScope(file_uuids=[str(doc.uuid)])

    resolved = resolve_scope_file_uuids(db_session, _ctx(normal_user), scope)

    assert resolved == []


def test_the_estimator_counts_a_document_only_selection(db_session, normal_user):
    """The reported symptom: selecting a document showed '0 recordings'. The
    estimator (``count_scope_files``) must report 1 for a scope naming exactly
    one accessible document.
    """
    doc = _make_document(db_session, normal_user)
    scope = ChatScope(file_uuids=[str(doc.uuid)])

    assert count_scope_files(db_session, _ctx(normal_user), scope) == 1


def test_a_document_uuid_that_does_not_exist_is_skipped_not_raised(db_session, normal_user):
    scope = ChatScope(file_uuids=[str(uuid_pkg.uuid4())])

    resolved = resolve_scope_file_uuids(db_session, _ctx(normal_user), scope)

    assert resolved == []
