"""Write path for the ``transcripts`` index — one full-document row per media file.

**This module WRITES; it does not search** (issue #542). It carried a
``search_transcripts`` that had zero callers anywhere in the tree, and whose
``use_semantic`` branch — defaulting to **True** — built an ANN ``knn`` query that
could never have succeeded:

* ``transcripts.embedding`` is mapped ``knn_vector`` with **no ``method`` block**, so
  the field has no HNSW graph. Measured live:
  ``400 … Field 'embedding' is not built for ANN search``.
* No document carries an ``embedding`` at all — ``index_transcript`` omits the field
  when it is ``None``, and every caller passes ``None``.
* On any embedding failure it fell back to a **zero vector**, which ``cosinesimil``
  rejects outright.

It survived because a **different** function with the same name — the real HTTP
endpoint in ``api/endpoints/search.py`` — is live and heavily used, so grepping the
name found working code one directory away. It was deleted rather than repaired: the
capability was already provided elsewhere (``files/filtering.py`` builds its own
keyword query against this index for the gallery filter), so keeping a second,
broken implementation bought nothing.

Dead, but not harmless while it lasted: the 400 carries the literal string
``search_phase_execution_exception``, which ``_is_index_corruption_error`` matches, so
the first cut of the #540 kNN health probe classified this index as **corrupt**.

Who still uses this index — it is very much alive:

* written here on every transcription (``tasks/transcription/{postprocess,background}``,
  ``tasks/search_indexing_task``) and retitled from ``files/crud``;
* read directly by ``api/endpoints/files/filtering.py`` (the gallery's
  transcript-content filter), ``opensearch_service/speaker_metadata.py``, the
  integrity task and ``file_cleanup_service``.

**Semantic retrieval is `transcript_chunks`**, through
``services/search/hybrid_search_service.py``. The vestigial ``embedding`` field in
this index's mapping should go on the next index version bump.
"""

import datetime
import logging

from app.core.config import settings
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.indices import ensure_indices_exist

logger = logging.getLogger(__name__)


def index_transcript(
    file_id: int,
    file_uuid: str,
    user_id: int,
    transcript_text: str,
    speakers: list[str],
    title: str,
    tags: list[str] | None = None,
    embedding: list[float] | None = None,
):
    """
    Index a transcript in OpenSearch

    Args:
        file_id: ID of the media file (for internal queries)
        file_uuid: UUID of the media file (used as document ID)
        user_id: ID of the user who owns the file
        transcript_text: Full transcript text
        speakers: List of speaker names/IDs in the transcript
        title: Title of the media file (filename)
        tags: Optional list of tags associated with the file
        embedding: Optional vector embedding of the transcript (if not provided, we'd compute it)
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping indexing")
        return

    try:
        ensure_indices_exist()

        # Skip embedding if not provided - let OpenSearch handle text search without vector similarity
        if embedding is None:
            logger.info(
                f"No embedding provided for transcript {file_uuid}, indexing with text search only"
            )
            # Don't include embedding field when none is provided

        # Prepare document
        doc = {
            "file_id": file_id,
            "file_uuid": str(file_uuid),
            "user_id": user_id,
            "content": transcript_text,
            "speakers": speakers,
            "title": title,
            "tags": tags or [],
            "upload_time": datetime.datetime.now(datetime.UTC).isoformat(),  # ISO-8601 format
        }

        # Only include embedding if provided
        if embedding is not None:
            doc["embedding"] = embedding

        # Index the document using UUID as document ID
        response = _client.opensearch_client.index(
            index=settings.OPENSEARCH_TRANSCRIPT_INDEX,
            body=doc,
            id=str(file_uuid),  # Use file_uuid as document ID
        )

        logger.info(f"Indexed transcript for file {file_uuid} (ID: {file_id}): {response}")
        return response

    except Exception as e:
        logger.error(f"Error indexing transcript for file {file_uuid} (ID: {file_id}): {e}")


def update_transcript_title(file_uuid: str, new_title: str):
    """
    Update the title of an indexed transcript in OpenSearch

    Args:
        file_uuid: UUID of the media file
        new_title: New title to update
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping title update")
        return

    try:
        # Update the document with the new title
        update_body = {"doc": {"title": new_title}}

        response = _client.opensearch_client.update(
            index=settings.OPENSEARCH_TRANSCRIPT_INDEX,
            id=str(file_uuid),
            body=update_body,
        )

        logger.info(f"Updated transcript title for file {file_uuid}: {response}")
        return response

    except Exception as e:
        # If the document doesn't exist yet, that's okay - it will be indexed later
        if "not_found" in str(e).lower():
            logger.info(
                f"Document not found for file {file_uuid}, will be indexed when transcription completes"
            )
        else:
            logger.error(f"Error updating transcript title for file {file_uuid}: {e}")
