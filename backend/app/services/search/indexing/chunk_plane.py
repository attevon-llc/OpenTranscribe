"""Chunk plane of ``TranscriptIndexingService``: per-file delete, erasure count, stale-tail prune."""

import logging

from app.core.config import settings
from app.services.opensearch_service import opensearch_client

from ..plane_queries import chunk_plane_query
from ..plane_queries import file_plane_query
from .orphans import OrphanProbeMixin

logger = logging.getLogger(__name__)


class ChunkPlaneMixin(OrphanProbeMixin):
    """Delete, count and prune operations over a file's chunk documents."""

    def delete_transcript_chunks(self, file_uuid: str) -> int:
        """Delete **every** indexed document for a file — both planes.

        Its two callers are file deletion and the full rebuild in
        :meth:`reindex_transcript`, and both mean "leave nothing behind":
        a digest that outlives its file is a readable summary of a deleted
        recording, and a digest that survives a rebuild whose transcript
        re-sectioned to fewer parts is a stale orphan (#400's failure, one
        plane over). Hence :func:`file_plane_query`, not
        :func:`chunk_plane_query`.

        Args:
            file_uuid: UUID of the file to delete documents for.

        Returns:
            Number of documents deleted.
        """
        if not opensearch_client:
            return 0

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        try:
            if not opensearch_client.indices.exists(index=index_name):
                return 0

            response = opensearch_client.delete_by_query(
                index=index_name,
                body={"query": file_plane_query(file_uuid)},
                refresh=True,
            )
            deleted: int = response.get("deleted", 0)
            logger.info(f"Deleted {deleted} chunks for file {file_uuid}")
            if deleted:
                # Function-local: chat.retrieval_cache imports search.chunk_retrieval at top level.
                from app.services.chat.retrieval_cache import bump_corpus_version

                bump_corpus_version()
            return deleted
        except Exception as e:
            logger.error(f"Error deleting chunks for file {file_uuid}: {e}")
            return 0

    def count_file_documents(self, file_uuid: str) -> int:
        """Count this file's still-indexed documents — **every plane**.

        The verification half of :meth:`delete_transcript_chunks`, and it lives
        here rather than at the caller so the two share one predicate: a survivor
        count built from a *different* predicate than the delete it checks is not
        a check. ``delete_transcript_chunks`` returns 0 for "no chunks", "index
        absent" and "the delete failed" alike, so this count — never that return
        value — is what proves an erasure complete.

        **Both planes, deliberately** (index v6, #403 Stage 3). A
        :func:`chunk_plane_query` count would report a clean sweep while the
        file's ``doc_type: digest`` sections were still indexed, and those
        sections are verbatim transcript text: the erasure would audit as
        complete with the recording's own words still searchable and still
        retrievable by chat. That is the failure the unqualified delete exists to
        prevent, arriving one step later through its verifier.

        Unlike everything else in this class it **raises** rather than returning
        0 when it cannot ask. Its caller is a GDPR Art. 17 erasure that records
        "could not verify" as a residual, and a 0 meaning "the cluster was
        unreachable" is precisely the wrong answer there. An **absent index** is
        still 0 — that is the cluster answering.

        The explicit refresh is the #435 lesson applied to the delete path: a
        ``count`` is a search and sees only what the last refresh made visible,
        so without it a delete that raised part-way could leave documents that
        the count cannot see and reports as gone. One refresh per erased file is
        not on any hot path.

        Args:
            file_uuid: UUID of the media file.

        Returns:
            Number of documents of any plane still matching the file.

        Raises:
            Exception: The cluster could not be reached or could not answer.
        """
        from opensearchpy.exceptions import NotFoundError

        if not opensearch_client:
            raise RuntimeError("OpenSearch client unavailable")

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        try:
            if not opensearch_client.indices.exists(index=index_name):
                return 0
            opensearch_client.indices.refresh(index=index_name)
            response = opensearch_client.count(
                index=index_name, body={"query": file_plane_query(file_uuid)}
            )
            return int(response["count"])
        except NotFoundError:
            return 0

    def _prune_stale_chunks(self, file_uuid: str, *, keep_count: int) -> int:
        """Delete chunks left behind by a longer previous chunking of this file.

        Called after every bulk load. Chunks ``0..keep_count-1`` were just
        overwritten in place (the doc id is derived from ``chunk_index``); anything
        at or above ``keep_count`` belongs to a chunking that no longer exists.

        **The gate is an id probe, not a count, and that is issue #435.** A ``count``
        is a search, and a search sees only what the last ``refresh`` made visible,
        while the bulk load above uses ``refresh=False``. So a second index of the
        same file inside the refresh window used to find an empty tail, skip the
        delete, and leave the orphans **permanently** — nothing later re-examined
        them and a subsequent refresh triggered no prune.

        The previous version of this docstring called that unreachable ("no pipeline
        path re-indexes the same file twice inside one second"). Both halves of that
        were false, and both were measured rather than argued:

        * **Reachable, and not narrowly.** A back-to-back pair of
          ``index_transcript_chunks`` calls completes in 125–224 ms at this cluster's
          production ``refresh_interval``, and the stale tail survived **11 of 12
          pairs**.
        * **Nothing serialises the callers.** Five dispatch sites reach the
          single-file indexing task — ``tasks/transcription/enrich.py``,
          ``tasks/transcription/background.py``, ``services/task_recovery_service.py``,
          ``api/endpoints/files/reprocess.py`` (user-triggered, at any moment, with no
          in-flight or lock check) and ``scripts/corpus_injection/injector.py`` — plus
          the batch loop in ``tasks/reindex_task.py``. The recovery sweep is the
          sharpest: it branches on the index record being *missing*, not on the
          original task having *stopped*, so a file whose indexing is in flight gets a
          second dispatch by design.

        **What the mechanism now guarantees**, and only this: any orphan that exists
        when :meth:`_orphaned_document_ids` runs is found, whether or not a refresh
        has made it searchable. The probe is realtime; the ``refresh`` below is then
        required because ``delete_by_query`` executes as a **scroll search** and
        therefore cannot see what the probe just found — measured directly, a
        ``delete_by_query`` over 4 unrefreshed documents deletes 0. That is also why
        a ``_seq_no`` or ``_version`` predicate cannot replace the gate: a predicate
        changes which *visible* documents match, it cannot make invisible ones
        visible.

        **What it does NOT guarantee.** Two genuinely overlapping
        ``index_transcript_chunks`` calls for one file are still not serialised by
        anything. If a second call's probe runs before a first call's bulk load
        returns, the tail it has not written yet cannot be found — by any mechanism
        short of a lock. Interleaved writers on one file remain a coordination
        problem, not a visibility one.

        **Cost, measured rather than assumed** (real mapping, 384-d HNSW, 50
        chunks/file, 25 samples, at 5k/15k/30k documents): the probe is
        **4.0–4.8 ms** against the **3.7–4.1 ms** count it replaces. The alternative
        — refreshing unconditionally before the gate — is **95 ms median /
        116–184 ms mean per file**, near-flat in index size because it is dominated
        by building the HNSW graph for the vectors just written, which is **+41 s to
        +79 s on a 432-file reindex that takes 182 s**. Hence the refresh sits behind
        the gate, on the path where a re-chunk actually shrank, and a full reindex
        pays none of it.

        A failure here is logged, not raised: the new chunks are already indexed and
        correct, and the caller must not report a successful index as a failure.

        Args:
            file_uuid: UUID of the media file just indexed.
            keep_count: Number of chunks the new chunking produced.

        Returns:
            Number of stale chunks deleted.
        """
        if not opensearch_client:
            return 0

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        try:
            stale_ids = self._orphaned_document_ids(
                index_name=index_name,
                document_id=lambda n: f"{file_uuid}_{n}",
                first_orphan=keep_count,
                plane_query=chunk_plane_query(file_uuid),
                counter_field="chunk_index",
            )
            if not stale_ids:
                return 0

            # The probe read the translog; delete_by_query reads a searcher. Without
            # this the delete matches nothing it was just told about.
            opensearch_client.indices.refresh(index=index_name)
            response = opensearch_client.delete_by_query(
                index=index_name,
                body={"query": chunk_plane_query(file_uuid, from_chunk_index=keep_count)},
                refresh=True,
                conflicts="proceed",
            )
            deleted = int(response.get("deleted", 0))
            conflicts = int(response.get("version_conflicts", 0) or 0)
            logger.info(
                f"Pruned {deleted} stale chunk(s) for file {file_uuid} "
                f"(re-chunk shrank to {keep_count} chunks)"
            )
            # `deleted != len(stale_ids)` has THREE causes and this used to
            # attribute all of them to the first, so a real conflict read as a
            # predicate bug:
            #   fewer  — the predicate declined documents the probe found. The known
            #            cause is a lost pre-v6 compat arm (chunks written before
            #            index v6 carry no `doc_type`), or documents skipped by
            #            `conflicts="proceed"` — which `conflicts` distinguishes.
            #   more   — the range delete removed documents the probe did not
            #            enumerate. Normal and healthy: the probe is a boolean gate
            #            over a windowed id walk, the delete is a range.
            if conflicts:
                logger.warning(
                    f"Stale-chunk prune for file {file_uuid} skipped {conflicts} document(s) "
                    "on version conflict; they keep their stale content until the next prune"
                )
            elif deleted < len(stale_ids):
                logger.warning(
                    f"Stale-chunk prune for file {file_uuid} found {len(stale_ids)} "
                    f"orphan id(s) but the chunk-plane predicate deleted only {deleted} — "
                    "the predicate is narrower than the id probe"
                )
            return deleted
        except Exception as e:
            logger.error(
                f"Could not prune stale chunks for file {file_uuid} "
                f"(chunks >= {keep_count} may still be searchable): {e}"
            )
            return 0
