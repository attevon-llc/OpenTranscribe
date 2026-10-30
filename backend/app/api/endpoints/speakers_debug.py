"""Speaker maintenance and cross-media debug endpoints."""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session
from sqlalchemy.orm import load_only

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.api.error_handlers import ErrorHandler
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.models.user import User
from app.services.permission_service import file_ids_in_scope
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import hide_machine_identities
from app.services.request_context import RequestContext
from app.utils.speaker_identity_source import VOICEPRINT

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/cleanup-orphaned-embeddings", response_model=dict[str, Any])
def cleanup_orphaned_embeddings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Clean up orphaned speaker embeddings in OpenSearch for non-existent MediaFiles.
    """
    try:
        from app.services.opensearch_service import cleanup_orphaned_speaker_embeddings

        deleted_count = cleanup_orphaned_speaker_embeddings(current_user.id)

        return {
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Cleaned up {deleted_count} orphaned speaker embeddings",
        }
    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error during cleanup: {e}")
        raise ErrorHandler.internal_error() from e


def _collect_index_debug_documents(user_id: int) -> dict[str, Any]:
    """Read the speaker and profile documents for ``user_id`` out of the search index.

    **Takes no ``Session``.** Two OpenSearch searches (100 hits each) used to run on
    the request session in ``debug_cross_media_data``, so a slow or unreachable
    cluster held the request's Postgres transaction — and therefore ACCESS SHARE on
    every table it had read — for the whole round trip. Returns plain dicts;
    the caller merges them into its report.

    The diagnostic contract is unchanged: a failure is reported in the payload
    (``opensearch_error``) rather than failing the endpoint.
    """
    section: dict[str, Any] = {"opensearch_speakers": [], "opensearch_profiles": []}
    try:
        from app.core.embedding_models import get_speaker_index
        from app.services.opensearch_service import opensearch_client

        if not opensearch_client:
            return section

        speaker_index = get_speaker_index()
        # Query all speaker documents for this user
        query = {
            "size": 100,
            "query": {
                "bool": {
                    "must": [{"term": {"user_id": user_id}}],
                    "must_not": [
                        {"exists": {"field": "document_type"}}
                    ],  # Only speakers, not profiles
                }
            },
        }

        response = opensearch_client.search(index=speaker_index, body=query)
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            section["opensearch_speakers"].append(
                {
                    "opensearch_id": hit["_id"],
                    "speaker_id": source.get("speaker_id"),
                    "display_name": source.get("display_name"),
                    "profile_id": source.get("profile_id"),
                    "media_file_id": source.get("media_file_id"),
                    "user_id": source.get("user_id"),
                }
            )

        # Query profile documents
        profile_query = {
            "size": 100,
            "query": {
                "bool": {
                    "must": [
                        {"term": {"user_id": user_id}},
                        {"term": {"document_type": "profile"}},
                    ]
                }
            },
        }

        profile_response = opensearch_client.search(index=speaker_index, body=profile_query)
        for hit in profile_response["hits"]["hits"]:
            source = hit["_source"]
            section["opensearch_profiles"].append(
                {
                    "opensearch_id": hit["_id"],
                    "profile_id": source.get("profile_id"),
                    "profile_name": source.get("profile_name"),
                    "speaker_count": source.get("speaker_count"),
                    "user_id": source.get("user_id"),
                }
            )

    except Exception as e:
        # Diagnostic endpoint: the OpenSearch section is optional, and the
        # error is surfaced in the payload rather than failing the report.
        # `section` is merged into the endpoint's response body by the caller
        # (`debug_info.update(_collect_index_debug_documents(...))`), so the raw
        # opensearch-py exception text would be returned verbatim — and it quotes the
        # cluster URL (`http://opensearch:9200`), the index name, and on a transport
        # failure the full request body (#914). The admin only needs to know the
        # section is unavailable and why in class terms; the detail is in the log.
        logger.exception("OpenSearch section of the speaker debug report failed")
        section["opensearch_error"] = f"OpenSearch section unavailable ({type(e).__name__})"

    return section


@router.get("/debug/cross-media-data", response_model=dict[str, Any])
def debug_cross_media_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """
    Debug endpoint to examine cross-media matching data in PostgreSQL and OpenSearch.
    """
    try:
        debug_info: dict[str, Any] = {
            "user_id": current_user.id,
            "media_files": [],
            "speakers": [],
            "profiles": [],
            "opensearch_speakers": [],
            "opensearch_profiles": [],
        }

        # Get all media files for this user (project only needed fields)
        media_rows = (
            db.query(MediaFile.id, MediaFile.filename, MediaFile.title, MediaFile.status)
            .filter(MediaFile.user_id == current_user.id)
            .all()
        )
        for mf in media_rows:
            debug_info["media_files"].append(
                {
                    "id": mf.id,
                    "filename": mf.filename,
                    "title": mf.title,
                    "status": mf.status.value if mf.status else None,
                }
            )

        # Get all speakers for this user (project only needed fields)
        # Entities (not bare columns) so the privacy view can withhold what a voiceprint
        # made: a name or link of a file whose voiceprints are off is not reported (#1280).
        speaker_rows = (
            db.query(Speaker)
            .options(
                load_only(
                    Speaker.id,
                    Speaker.name,
                    Speaker.display_name,
                    Speaker.profile_id,
                    Speaker.media_file_id,
                    Speaker.verified,
                    Speaker.confidence,
                    Speaker.identity_source,
                )
            )
            .filter(Speaker.user_id == current_user.id)
            .order_by(Speaker.media_file_id, Speaker.id)
            .all()
        )
        # id -> identity_source, read now: the commit below expires the loaded rows.
        withheld = {
            int(s.id): s.identity_source
            for s in hide_machine_identities(PrivacyView(db), speaker_rows)
        }

        for s in speaker_rows:
            debug_info["speakers"].append(
                {
                    "id": s.id,
                    "name": s.name,
                    "display_name": s.display_name,
                    "profile_id": s.profile_id,
                    "media_file_id": s.media_file_id,
                    "verified": s.verified,
                    "confidence": None if s.id in withheld else s.confidence,
                }
            )

        # Get all speaker profiles
        profiles = db.query(SpeakerProfile).filter(SpeakerProfile.user_id == current_user.id).all()
        for profile in profiles:
            debug_info["profiles"].append(
                {
                    "id": profile.id,
                    "name": profile.name,
                    "uuid": profile.uuid,
                    "description": profile.description,
                }
            )

        # Every Postgres read is done. End the request's transaction BEFORE the
        # search-index round trips below: a plain SELECT holds ACCESS SHARE for the
        # life of its transaction, so waiting on a slow cluster here would queue
        # ALTER TABLE (an Alembic upgrade) and pin the vacuum horizon for nothing.
        # This handler performs no writes, so the commit only closes the read.
        db.commit()

        # Get the index-side view. No session is held across it — the helper does
        # not take one.
        index_documents = _collect_index_debug_documents(current_user.id)
        for document in index_documents.get("opensearch_speakers", []):
            if document.get("speaker_id") in withheld:
                document["profile_id"] = None
                if withheld[document["speaker_id"]] == VOICEPRINT:
                    document["display_name"] = None
        debug_info.update(index_documents)

        # Add summary analysis
        debug_info["analysis"] = {
            "total_media_files": len(debug_info["media_files"]),
            "total_speakers": len(debug_info["speakers"]),
            "total_profiles": len(debug_info["profiles"]),
            "opensearch_speaker_count": len(debug_info["opensearch_speakers"]),
            "opensearch_profile_count": len(debug_info["opensearch_profiles"]),
        }

        return debug_info

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error in debug endpoint: {e}")
        raise ErrorHandler.internal_error() from e


@router.get("/debug/cross-media-by-name", response_model=dict[str, Any])
def debug_cross_media_by_name(
    speaker_name: str = Query(..., description="Speaker display name to search for"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """
    Debug endpoint to test cross-media logic for a specific speaker name.

    The route is admin-gated, but it returns speaker names and file titles, so it stays
    inside the request's active tenant, and reads other users' rows there only where the
    request's bypass sees all of the scope (single-tenant mode, or an organization
    support-access grant). Otherwise it is the caller's own.
    """
    try:
        in_scope = Speaker.media_file_id.in_(file_ids_in_scope(ctx.org_id))
        # Find all matching speakers
        query = db.query(Speaker).filter(Speaker.display_name == speaker_name)
        if not ctx.bypass.sees_all_in_scope:
            query = query.filter(Speaker.user_id == current_user.id)
        query = query.filter(in_scope)
        # Matching is on the name and link the caller may see (issue #1280): each row is
        # judged by its own file's switch, and a voiceprint-made name is not a match.
        privacy = PrivacyView(db)
        matching_speakers = query.all()
        hide_machine_identities(privacy, matching_speakers)
        matching_speakers = [s for s in matching_speakers if s.display_name == speaker_name]

        results: dict[str, Any] = {
            "speakers_found": len(matching_speakers),
            "cross_media_results": [],
        }

        for speaker in matching_speakers:
            # Test the cross-media logic for this speaker
            cross_media_result: dict[str, Any] = {
                "speaker_id": speaker.id,
                "speaker_name": speaker.name,
                "media_file_id": speaker.media_file_id,
                "profile_id": speaker.profile_id,
                "verified": speaker.verified,
                "occurrences": [],
            }

            # Replicate the cross-media logic
            if speaker.profile_id:
                # Speaker has a profile - find all instances of this profile
                profile_q = (
                    db.query(Speaker)
                    .join(MediaFile)
                    .filter(
                        Speaker.profile_id == speaker.profile_id,
                    )
                )
                if not ctx.bypass.sees_all_in_scope:
                    profile_q = profile_q.filter(Speaker.user_id == current_user.id)
                profile_q = profile_q.filter(in_scope)
                profile_speakers = profile_q.all()
                hide_machine_identities(privacy, profile_speakers)
                profile_speakers = [
                    p for p in profile_speakers if p.profile_id == speaker.profile_id
                ]

                cross_media_result["method_used"] = "profile_based"
                cross_media_result["profile_speakers_found"] = len(profile_speakers)

                for profile_speaker in profile_speakers:
                    media_file = profile_speaker.media_file
                    if media_file:
                        occurrence = {
                            "speaker_id": profile_speaker.id,
                            "media_file_id": media_file.id,
                            "media_file_title": media_file.title or media_file.filename,
                            "same_speaker": profile_speaker.id == speaker.id,
                        }
                        cross_media_result["occurrences"].append(occurrence)

            else:
                # Speaker has no profile - search by display_name
                similar_q = (
                    db.query(Speaker)
                    .join(MediaFile)
                    .filter(
                        Speaker.display_name == speaker.display_name,
                        Speaker.id != speaker.id,  # Exclude self
                    )
                )
                if not ctx.bypass.sees_all_in_scope:
                    similar_q = similar_q.filter(Speaker.user_id == current_user.id)
                similar_q = similar_q.filter(in_scope)
                similar_speakers = similar_q.all()
                hide_machine_identities(privacy, similar_speakers)
                similar_speakers = [
                    p for p in similar_speakers if p.display_name == speaker.display_name
                ]

                cross_media_result["method_used"] = "display_name_based"
                cross_media_result["similar_speakers_found"] = len(similar_speakers)

                # Add self first
                if speaker.media_file:
                    cross_media_result["occurrences"].append(
                        {
                            "speaker_id": speaker.id,
                            "media_file_id": speaker.media_file.id,
                            "media_file_title": speaker.media_file.title
                            or speaker.media_file.filename,
                            "same_speaker": True,
                        }
                    )

                # Add similar speakers
                for similar_speaker in similar_speakers:
                    similar_media_file = similar_speaker.media_file
                    if similar_media_file:
                        occurrence = {
                            "speaker_id": similar_speaker.id,
                            "media_file_id": similar_media_file.id,
                            "media_file_title": similar_media_file.title
                            or similar_media_file.filename,
                            "same_speaker": False,
                        }
                        cross_media_result["occurrences"].append(occurrence)

            results["cross_media_results"].append(cross_media_result)

        return results

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error in cross-media-by-name debug endpoint: {e}")
        raise ErrorHandler.internal_error() from e
