"""Transcript indexing service for OpenSearch chunk-level search."""

import datetime
import logging
import time
from typing import Any

from app.core.config import settings
from app.services.ingest_artifacts import index_mapping as digest_mapping
from app.services.opensearch_service import get_opensearch_client

from .chunking_service import chunk_transcript_by_speaker_turns
from .corpus_cache import _invalidate_chat_retrieval_cache
from .embedding_provenance import active_embedding_model
from .index_admin import ensure_chunks_index_exists
from .indexing.bulk import _suspended_refresh_for_large_index
from .indexing.chunk_plane import ChunkPlaneMixin
from .indexing.digest_plane import DigestPlaneMixin
from .indexing.summary_plane import SummaryPlaneMixin
from .pipelines import ensure_search_pipeline_exists
from .pipelines import is_neural_pipeline_available

logger = logging.getLogger(__name__)


class TranscriptIndexingService(ChunkPlaneMixin, DigestPlaneMixin, SummaryPlaneMixin):
    """Handles chunking, embedding, and indexing transcripts into OpenSearch.

    Uses OpenSearch neural search for embedding generation. Embeddings are
    generated server-side via the neural ingest pipeline, which eliminates
    Python embedding overhead and enables hot-swap model changes.

    If neural search is not available, documents are indexed without embeddings
    and search falls back to BM25-only (keyword search).
    """

    def index_transcript_chunks(
        self,
        file_id: int,
        file_uuid: str,
        user_id: int,
        segments: list[dict[str, Any]],
        title: str,
        speakers: list[str],
        tags: list[str],
        upload_time: str | None = None,
        language: str = "en",
        content_type: str = "",
        duration: float | None = None,
        file_size: int | None = None,
        collection_ids: list[int] | None = None,
        accessible_user_ids: list[int] | None = None,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """Chunk and index a transcript.

        Embedding modes (in priority order):
        1. Neural pipeline available — OpenSearch generates embeddings server-side
        2. No embedding — BM25 keyword search only

        Args:
            file_id: Media file integer ID.
            file_uuid: Media file UUID.
            user_id: Owner user ID.
            segments: Transcript segments with start, end, text, speaker.
            title: File title.
            speakers: All speaker names in the file.
            tags: Tags associated with the file.
            upload_time: Upload time ISO string (defaults to now).
            language: Language code.
            content_type: MIME content type of the file.
            duration: Duration in seconds.
            file_size: File size in bytes.
            collection_ids: List of collection IDs the file belongs to.
            accessible_user_ids: List of user IDs with access to this file.
                Includes owner + users/groups with collection shares.
                If None, defaults to [user_id] (owner only).

        Returns:
            Dict of indexing stats. ``chunk_count`` 0 with a ``reason`` means there
            was legitimately nothing to index; a FAILURE raises (issue #495).
        """
        # These two return a dict with an explicit `reason`, not a bare 0 (issue #495).
        # Both are legitimate "nothing to index" outcomes — and while this method also
        # swallowed its exceptions into `return 0`, a real failure was *indistinguishable
        # from them*. That is what made a dead OpenSearch report a successful index.
        # Naming the reason is what lets zero-because-nothing-to-do and
        # zero-because-it-broke be told apart at all; the latter now raises.
        client = get_opensearch_client()
        if not client:
            logger.warning("OpenSearch client not initialized, skipping chunk indexing")
            return {"chunk_count": 0, "reason": "no_opensearch_client"}

        if not segments:
            logger.warning(f"No segments to index for file {file_uuid}")
            return {"chunk_count": 0, "reason": "no_segments"}

        ensure_chunks_index_exists()
        ensure_search_pipeline_exists()

        if upload_time is None:
            upload_time = datetime.datetime.now(datetime.UTC).isoformat()

        # 1. Chunk segments
        t_chunk_start = time.time()
        chunks = chunk_transcript_by_speaker_turns(
            segments=segments,
            file_uuid=file_uuid,
            file_id=file_id,
            user_id=user_id,
            title=title,
            speakers=speakers,
            tags=tags,
            upload_time=upload_time,
            language=language,
            content_type=content_type,
            duration=duration,
            file_size=file_size,
            collection_ids=collection_ids,
            organization_id=organization_id,
        )
        chunk_ms = round((time.time() - t_chunk_start) * 1000)

        if not chunks:
            # A transcript that now yields NO chunks still has to lose the ones it
            # used to have. `reindex_transcript` deletes first and is safe, but it
            # is not the primary path — `tasks/search_indexing_task` calls this
            # method directly, and the segments-exist-but-chunk-to-nothing case
            # (every segment empty after cleanup) reaches here rather than the
            # `not segments` guard above. Without this, those chunks stayed
            # searchable forever with no way to notice.
            pruned = self._prune_stale_chunks(file_uuid, keep_count=0)
            logger.warning(
                f"No chunks generated for file {file_uuid}; pruned {pruned} stale chunk(s)"
            )
            return {"chunk_count": 0, "reason": "no_chunks_generated", "stale_removed": pruned}

        # 2. Add indexed_at timestamp, accessible_user_ids, and the v6 fields
        now = datetime.datetime.now(datetime.UTC).isoformat()
        effective_user_ids = accessible_user_ids if accessible_user_ids else [user_id]
        header_roster = sorted(speakers or [])
        for chunk in chunks:
            chunk["indexed_at"] = now
            chunk["accessible_user_ids"] = effective_user_ids
            chunk[digest_mapping.DOC_TYPE_FIELD] = digest_mapping.DOC_TYPE_CHUNK
            # The pipeline embeds this field, not `content` — see
            # `_build_neural_ingest_pipeline`. BM25 still scores `content`.
            chunk["embedding_text"] = digest_mapping.build_embedding_text(
                title=title,
                recorded_at=upload_time,
                roster=header_roster,
                body=str(chunk.get("content") or ""),
            )

        # 3. Choose embedding mode and index
        t_index_start = time.time()
        try:
            use_neural = is_neural_pipeline_available()
            # The model that will actually produce these vectors, not the mode
            # they were produced in (#437). ``is_neural_pipeline_available`` is
            # what verifies the pipeline, so the label is resolved by the time we
            # read it; when it could not be named this is the same ``"neural"``
            # every pre-#437 document carries, so unknown stays one bucket.
            provenance = active_embedding_model()
            if use_neural:
                for chunk in chunks:
                    chunk["embedding_model"] = provenance
                logger.debug(f"Using neural ingest pipeline for file {file_uuid} ({provenance})")
            else:
                for chunk in chunks:
                    chunk["embedding_model"] = None
                logger.warning(f"Neural pipeline not available for {file_uuid}, text-only")

            # For very large transcripts (6h+ recordings produce 500+ chunks)
            # suspend index refresh during the bulk load so we don't pay the
            # per-batch refresh cost. The context manager restores the prior
            # refresh_interval on exit.
            with _suspended_refresh_for_large_index(
                settings.OPENSEARCH_CHUNKS_INDEX,
                chunk_count=len(chunks),
                threshold=settings.SEARCH_LARGE_TRANSCRIPT_CHUNKS,
            ):
                indexed = self._bulk_index_chunks(chunks, use_neural_pipeline=use_neural)

            # A PARTIAL index must fail the task (issue #495). `_bulk_index_chunks`
            # returns how many documents actually landed, and until this check existed
            # nothing compared it to how many were built: `_retry_failed_docs` gives up
            # after 2 attempts, logs "N documents failed after 2 retries", and returns —
            # and this method then reported `status: success` with `chunk_count` quietly
            # short. The file is left permanently half-searchable, with the missing
            # chunks unreachable by search and by RAG chat, and the only trace is one
            # ERROR line in a worker log nobody is reading.
            #
            # Raising is the honest outcome and is safe to retry: document ids are
            # deterministic (`{file_uuid}_{chunk_index}`), so a re-run overwrites rather
            # than duplicates. It is also strictly better than the alternative of
            # continuing — a task that reports success is never retried by anything.
            if indexed < len(chunks):
                raise RuntimeError(
                    f"Partial chunk index for file {file_uuid}: {indexed} of "
                    f"{len(chunks)} documents landed after retries. The file would "
                    "otherwise be reported as indexed while part of the transcript is "
                    "unreachable by search and RAG chat."
                )

            # Doc ids are deterministic (``{file_uuid}_{chunk_index}``), so the bulk
            # load above OVERWRITES chunks 0..len(chunks)-1 but cannot touch a longer
            # previous chunking's tail. Without this, a re-index after a segment merge,
            # a speaker-turn change or a SEARCH_CHUNK_TARGET_WORDS bump leaves stale
            # documents — old text, old speakers, old timestamps — that keep surfacing
            # in search and in RAG chat retrieval (issue #400).
            stale_removed = self._prune_stale_chunks(file_uuid, keep_count=len(chunks))

            # The digest plane (#403 Stage 3, addendum G1). It rides the chunk
            # index rather than the coordinator because `delete_transcript_chunks`
            # is an unqualified per-file delete: every rebuild trigger — version
            # bump, model switch, maintenance repair, manual reindex — comes
            # through here, and any one of them that regenerated chunks and not
            # digests would destroy the digest tier permanently.
            # Shared by the digest AND summary planes (issue #963) — one dict, so
            # the two provably cannot drift apart into two copies of the same
            # per-file metadata.
            base_metadata: dict[str, Any] = {
                "user_id": user_id,
                "title": title,
                "tags": tags or [],
                "upload_time": upload_time,
                "language": language,
                "content_type": content_type,
                "duration": duration,
                "file_size": file_size,
                "collection_ids": collection_ids or [],
                "accessible_user_ids": effective_user_ids,
                "indexed_at": now,
                "embedding_model": provenance if use_neural else None,
                **({} if organization_id is None else {"organization_id": organization_id}),
            }

            digest_count = self._index_digest_plane(
                file_id=file_id,
                file_uuid=file_uuid,
                base_metadata=base_metadata,
                use_neural=use_neural,
            )

            # The summary plane (issue #963). Rides the same per-file index path
            # as the digest plane and for the identical reason (addendum G1):
            # `delete_transcript_chunks` is an unqualified per-file delete, and
            # every rebuild trigger routes through here, so a summary plane not
            # regenerated on this path would be destroyed permanently by the
            # first version bump / model switch / manual reindex.
            summary_count = self._index_summary_plane(
                file_id=file_id,
                file_uuid=file_uuid,
                base_metadata=base_metadata,
                use_neural=use_neural,
            )

            index_ms = round((time.time() - t_index_start) * 1000)
            total_ms = chunk_ms + index_ms
            mode_str = "neural" if use_neural else "text-only"
            logger.info(
                f"Indexed {indexed} chunks for file {file_uuid} "
                f"(mode: {mode_str}, chunk={chunk_ms}ms, index={index_ms}ms)"
            )
            # New/changed transcript content: chat must not keep serving
            # retrieval results computed against the previous version.
            _invalidate_chat_retrieval_cache()
            return {
                "chunk_count": indexed,
                "chunk_ms": chunk_ms,
                "index_ms": index_ms,
                "total_ms": total_ms,
                "mode": mode_str,
                "neural": use_neural,
                "stale_removed": stale_removed,
                "digest_sections": digest_count,
                "summary_leaves": summary_count,
            }
        except Exception:
            # DO NOT swallow this into `return 0` (issue #495). It used to, and the
            # consequence was not a degraded result — it was a FALSE one. The caller,
            # `tasks/search_indexing_task`, has an int arm that wraps a bare count as
            # `{"chunk_count": result}`, marks the DB task **completed**, and returns
            # `{"status": "success", ...}`. So every failure in this method — a dead
            # OpenSearch, a mapping rejection, a partial bulk load — was reported to the
            # user, the task table and the notification as a successful index of zero
            # chunks. The task's own `except` (search_indexing_task.py, "Search indexing
            # failed for file …") could never fire, because nothing ever reached it.
            #
            # `logger.error` + a sentinel return is a reasonable pattern where the caller
            # inspects the sentinel. Here nothing did, and the sentinel was
            # indistinguishable from the legitimate zero-chunk case.
            logger.exception(f"Bulk indexing failed for file {file_uuid}")
            raise

    def reindex_transcript(
        self,
        file_id: int,
        file_uuid: str,
        user_id: int,
        segments: list[dict[str, Any]],
        title: str,
        speakers: list[str],
        tags: list[str],
        upload_time: str | None = None,
        language: str = "en",
        content_type: str = "",
        duration: float | None = None,
        file_size: int | None = None,
        collection_ids: list[int] | None = None,
        accessible_user_ids: list[int] | None = None,
        organization_id: int | None = None,
    ) -> int:
        """Re-chunk and re-index a single transcript.

        Args:
            Same as index_transcript_chunks.

        Returns:
            Number of chunks indexed.
        """
        # Delete every plane first. Not redundant with the tail prunes
        # ``index_transcript_chunks`` does (issue #400): a full rebuild wants a
        # clean slate, and this clears **every** plane rather than the chunk tail —
        # notably the digest plane, which is regenerated by the same call
        # (addendum G1).
        #
        # This used to claim it was "the only path that also clears the documents
        # of a transcript that now yields NO chunks", which was true and mattered,
        # because `reindex_transcript` is NOT the primary path —
        # `tasks/search_indexing_task` calls `index_transcript_chunks` directly.
        # That method now prunes on its own zero-chunk branch, so the claim is
        # obsolete rather than merely narrow.
        self.delete_transcript_chunks(file_uuid)

        # Re-index
        result = self.index_transcript_chunks(
            file_id=file_id,
            file_uuid=file_uuid,
            user_id=user_id,
            segments=segments,
            title=title,
            speakers=speakers,
            tags=tags,
            upload_time=upload_time,
            language=language,
            content_type=content_type,
            duration=duration,
            file_size=file_size,
            collection_ids=collection_ids,
            accessible_user_ids=accessible_user_ids,
            organization_id=organization_id,
        )
        chunk_count: int = result.get("chunk_count", 0)
        return chunk_count
