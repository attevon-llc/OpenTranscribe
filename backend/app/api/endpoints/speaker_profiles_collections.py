"""Speaker collection endpoints (split from speaker_profiles)."""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.deps_context import scope_to_context
from app.api.endpoints.auth import get_current_active_user
from app.api.error_handlers import ErrorHandler
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.speaker import SpeakerCollection
from app.models.speaker import SpeakerCollectionMember
from app.models.user import User
from app.services.request_context import RequestContext
from app.utils.db_helpers import org_stamp_is

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/collections", response_model=list[dict[str, Any]])
def list_speaker_collections(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """List the caller's speaker collections in the active tenant."""
    try:
        # Speaker collections are owner-scoped: the caller's own, in this tenant.
        collections = (
            scope_to_context(db.query(SpeakerCollection), SpeakerCollection, ctx)
            .filter(SpeakerCollection.user_id == current_user.id)
            .all()
        )

        # Batch-fetch member counts (avoids N+1 per-collection queries)
        from sqlalchemy import func as sa_func

        collection_ids = [c.id for c in collections]
        count_rows = (
            db.query(
                SpeakerCollectionMember.collection_id,
                sa_func.count().label("cnt"),
            )
            .filter(SpeakerCollectionMember.collection_id.in_(collection_ids))
            .group_by(SpeakerCollectionMember.collection_id)
            .all()
            if collection_ids
            else []
        )
        member_counts = {r.collection_id: r.cnt for r in count_rows}

        result = []
        for collection in collections:
            # Nullable timestamps on a persisted row (see the profiles listing above):
            # reported as null rather than asserted, so one bad row cannot 500 the list.
            result.append(
                {
                    "uuid": str(collection.uuid),
                    "name": collection.name,
                    "description": collection.description,
                    "is_public": collection.is_public,
                    "created_at": collection.created_at.isoformat()
                    if collection.created_at
                    else None,
                    "updated_at": collection.updated_at.isoformat()
                    if collection.updated_at
                    else None,
                    "member_count": member_counts.get(collection.id, 0),
                }
            )

        return result

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error listing speaker collections: {e}")
        raise ErrorHandler.internal_error() from e


@router.post(
    "/collections",
    response_model=dict[str, Any],
    dependencies=[Depends(refuse_under_support_grant)],
)
def create_speaker_collection(
    name: str,
    description: str | None = None,
    is_public: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Create a new speaker collection (stamped with the active tenant)."""
    try:
        # Check for name conflicts
        existing = (
            db.query(SpeakerCollection)
            .filter(
                SpeakerCollection.user_id == current_user.id,
                SpeakerCollection.name == name,
                org_stamp_is(SpeakerCollection.organization_id, ctx.org_id),
            )
            .first()
        )

        if existing:
            raise HTTPException(status_code=400, detail="Collection with this name already exists")

        collection = SpeakerCollection(
            user_id=current_user.id,
            name=name,
            description=description,
            is_public=is_public,
            organization_id=ctx.org_id,
        )

        db.add(collection)
        db.commit()
        db.refresh(collection)

        # created_at/updated_at populated by server_default after refresh
        assert collection.created_at is not None
        assert collection.updated_at is not None
        return {
            "uuid": str(collection.uuid),
            "name": collection.name,
            "description": collection.description,
            "is_public": collection.is_public,
            "created_at": collection.created_at.isoformat(),
            "updated_at": collection.updated_at.isoformat(),
        }

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error creating speaker collection: {e}")
        db.rollback()
        raise ErrorHandler.internal_error() from e
