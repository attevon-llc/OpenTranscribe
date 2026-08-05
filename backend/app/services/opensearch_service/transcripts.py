"""Transcript document indexing and full-text search."""

import datetime
import logging
from typing import Any

from app.core.config import settings
from app.core.constants import SENTENCE_TRANSFORMER_DIMENSION
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.client import _get_sentence_transformer
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


def search_transcripts(  # noqa: C901
    query: str,
    user_id: int,
    speaker: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    use_semantic: bool = True,
) -> list[dict[str, Any]]:
    """
    Search for transcripts matching the query

    Args:
        query: Search query text
        user_id: ID of the user performing the search
        speaker: Optional speaker name to filter by
        tags: Optional list of tags to filter by
        limit: Maximum number of results to return
        use_semantic: Whether to use semantic (vector) search in addition to text search

    Returns:
        List of matching documents
    """
    # Return empty results when OpenSearch is disabled or unavailable
    if not settings.OPENSEARCH_ENABLED:
        logger.debug("OpenSearch is disabled, returning empty search results")
        return []
    if not _client.opensearch_client:
        logger.debug("OpenSearch client not initialized, returning empty search results")
        return []

    try:
        # Build the search query
        must_conditions: list[dict[str, Any]] = [
            {"term": {"user_id": user_id}}  # Restrict to user's files
        ]

        # Add full-text search
        if query:
            must_conditions.append({"match": {"content": {"query": query, "fuzziness": "AUTO"}}})

        # Add speaker filter if specified
        if speaker:
            must_conditions.append({"term": {"speakers": speaker}})

        # Add tags filter if specified
        if tags and len(tags) > 0:
            must_conditions.append({"terms": {"tags": tags}})

        # Construct basic search
        search_body = {
            "query": {"bool": {"must": must_conditions}},
            "size": limit,
            "_source": [
                "file_id",
                "file_uuid",
                "title",
                "content",
                "speakers",
                "tags",
                "upload_time",
            ],
            "highlight": {
                "fields": {
                    "content": {
                        "pre_tags": ["<em>"],
                        "post_tags": ["</em>"],
                        "fragment_size": 150,
                    }
                }
            },
        }

        # Add semantic search if requested
        if use_semantic and query:
            # Compute the query embedding using sentence-transformers
            try:
                embedding_model = _get_sentence_transformer()

                # Generate embedding for the query
                query_embedding = embedding_model.encode(query, normalize_embeddings=True).tolist()
                logger.info(f"Generated embedding for query: {query[:30]}...")
            except ImportError:
                logger.warning(
                    "sentence-transformers package not installed, using fallback embedding"
                )
                # Fallback to zero vector
                query_embedding = [0.0] * SENTENCE_TRANSFORMER_DIMENSION
            except Exception as e:
                logger.warning(f"Error generating query embedding: {e}")
                # Fallback to zero vector
                query_embedding = [0.0] * SENTENCE_TRANSFORMER_DIMENSION

            # Add kNN query
            knn_query: dict[str, Any] = {
                "knn": {"embedding": {"vector": query_embedding, "k": limit}}
            }

            # Combine text search with vector search
            search_body_query = search_body["query"]
            if isinstance(search_body_query, dict) and "bool" in search_body_query:
                search_body_query["bool"]["should"] = [knn_query]

        # Execute search
        response = _client.opensearch_client.search(
            index=settings.OPENSEARCH_TRANSCRIPT_INDEX, body=search_body
        )

        # Process results
        results = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            result = {
                "file_id": source["file_id"],
                "file_uuid": source.get("file_uuid"),
                "title": source["title"],
                "speakers": source["speakers"],
                "upload_time": source["upload_time"],
            }

            # Add highlighted snippet if available
            if "highlight" in hit and "content" in hit["highlight"]:
                result["snippet"] = "...".join(hit["highlight"]["content"])
            else:
                # Fallback to first part of content
                content = source.get("content", "")
                result["snippet"] = content[:150] + "..." if len(content) > 150 else content

            results.append(result)

        return results

    except Exception as e:
        logger.error(f"Error searching transcripts: {e}")
        return []
