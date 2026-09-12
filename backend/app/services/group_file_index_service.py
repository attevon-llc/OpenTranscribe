"""Reindex the files a group's membership change affects.

Single home for the file-set derivation ``groups.py``'s membership endpoints already
used inline (``_reindex_group_shared_files``), so a directory-driven membership change
(SCIM, LDAP/OIDC group mapping) can dispatch the exact same reindex the admin-UI path
does, instead of duplicating the query three times.

**Why this must run after the membership commit, never before or mid-transaction:**
:func:`~app.tasks.search_indexing_task.update_file_access_index` reads
``UserGroupMember`` rows to recompute ``accessible_user_ids`` on the affected OpenSearch
documents. If it were dispatched before the triggering commit, the Celery worker could
run before the new membership rows are visible and would rewrite the index from the
*pre-change* membership — observably worse than not dispatching at all, since a
subsequent read (search, RAG retrieval) would see a fresh-looking write that still
encodes stale access. Every call site here is therefore placed immediately after the
caller's own ``db.commit()``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.media import CollectionMember
from app.models.sharing import CollectionShare
from app.tasks.search_indexing_task import update_file_access_index


def get_group_shared_file_ids(db: Session, group_id: int) -> list[int]:
    """Media file ids reachable via a collection shared with *group_id*."""
    shared_collection_ids = [
        cs.collection_id
        for cs in db.query(CollectionShare.collection_id)
        .filter(CollectionShare.target_group_id == group_id)
        .all()
    ]
    if not shared_collection_ids:
        return []
    return [
        cm.media_file_id
        for cm in db.query(CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id.in_(shared_collection_ids))
        .distinct()
        .all()
    ]


def reindex_group_shared_files(db: Session, group_id: int) -> None:
    """Dispatch ``update_file_access_index`` for every file shared with *group_id*.

    Call this AFTER the membership change has been committed — see the module
    docstring. A no-op (no dispatch) when the group shares no collections.
    """
    file_ids = get_group_shared_file_ids(db, group_id)
    if file_ids:
        update_file_access_index.delay(file_ids)
