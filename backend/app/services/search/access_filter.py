"""The OpenSearch read-access clause: who may read a chunk.

A chunk is readable by a caller when it lists them in ``accessible_user_ids`` (owner, explicit
shares; rewritten by ``update_file_access_index``) **or**, inside an organization, when its
``collection_ids`` names an organization collection the caller is a member of and may see
(issue #1275, governance plan D-3). The second arm is computed per request in Postgres from the
caller's current visibility, so joining or leaving an organization, or switching a collection
to restricted, takes effect on the next query with no reindex.

Every transcript-plane reader builds its ACL term through :func:`access_filter_clause`; a bare
``{"terms": {"accessible_user_ids": [...]}}`` beside it would silently hide files the rest of the
app lets the caller open.
"""

import logging
from typing import Any

from app.core.constants import MAX_VISIBLE_COLLECTION_TERMS

logger = logging.getLogger(__name__)


def access_filter_clause(user_id: int, organization_id: int | None) -> dict[str, Any]:
    """The ``filter`` clause matching the chunks ``user_id`` may read in the tenant scope."""
    by_user: dict[str, Any] = {"terms": {"accessible_user_ids": [user_id]}}
    if organization_id is None:
        return by_user

    from app.db.session_utils import session_scope
    from app.services.collection_visibility import org_readable_collection_ids

    with session_scope() as db:
        collection_ids = org_readable_collection_ids(db, user_id, organization_id)
    if not collection_ids:
        return by_user
    if len(collection_ids) > MAX_VISIBLE_COLLECTION_TERMS:
        logger.warning(
            "User %s can read %d organization collections (cap %d); search falls back to "
            "per-file access only",
            user_id,
            len(collection_ids),
            MAX_VISIBLE_COLLECTION_TERMS,
        )
        return by_user
    return {
        "bool": {
            "should": [by_user, {"terms": {"collection_ids": collection_ids}}],
            "minimum_should_match": 1,
        }
    }
