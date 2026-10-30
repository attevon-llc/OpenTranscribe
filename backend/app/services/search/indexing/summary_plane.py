"""Summary plane of ``TranscriptIndexingService``: rebuild, index and prune summary leaves."""

import logging
from typing import Any

from app.core.config import settings
from app.services.ingest_artifacts import index_mapping as digest_mapping
from app.services.opensearch_service import opensearch_client

from ..plane_queries import summary_plane_query
from .bulk import BulkIndexMixin
from .orphans import OrphanProbeMixin

logger = logging.getLogger(__name__)


class SummaryPlaneMixin(BulkIndexMixin, OrphanProbeMixin):
    """Per-file summary-leaf indexing and its stale-tail prune."""

    def _index_summary_plane(
        self,
        *,
        file_id: int,
        file_uuid: str,
        base_metadata: dict[str, Any],
        use_neural: bool,
    ) -> int:
        """Rebuild this file's summary-leaf documents from Postgres (issue #963).

        ⚠️ There is deliberately **no ``summary_data`` parameter**. The summary
        is read here, from ``media_file``, on this method's own session. That
        is the whole anti-regression for #67: the retired implementation took
        the dict the summarization task had just produced and wrote it to the
        column and to OpenSearch in the same task, making two stores into two
        primaries. With no argument to pass a summary payload through, a
        caller cannot reproduce that shape even by accident — Postgres
        (``media_file.summary_data``) is canonical, OpenSearch is derived and
        fully rebuildable from it.

        Modelled line-for-line on :meth:`_index_digest_plane` — same own
        session, same "report what landed, not what was built" rule (#495),
        same "log, don't raise" asymmetry (a summary is derived enrichment;
        the chunks are the transcript itself).

        Args:
            file_id: Media file integer id.
            file_uuid: Media file UUID.
            base_metadata: Per-file fields shared with chunk/digest documents.
                The ACL rewrite keys on ``file_id`` and the tenant backfill on
                ``file_uuid`` (addendum G5), so ``build_summary_documents``
                puts **both** on every summary document — do not strip
                either.
            use_neural: Whether to run the documents through the ingest
                pipeline.

        Returns:
            Number of summary leaves indexed. ``0`` for a file with no
            summary — a valid outcome (#462 rejection 3, the no-LLM
            deployment), not a failure.
        """
        try:
            from app.db.session_utils import session_scope
            from app.models.media import MediaFile
            from app.services.ingest_artifacts import generate_file_artifacts
            from app.services.ingest_artifacts.service import hidden_speaker_names

            with session_scope() as db:
                media_file = db.query(MediaFile).filter(MediaFile.id == file_id).one_or_none()
                if media_file is None:
                    return 0
                # `reindex_task._load_reindex_page` defers `summary_data` on the
                # object it hands to the chunk-indexing path, so it is genuinely
                # not present there — reading it here, on this method's own
                # session, is required rather than redundant.
                summary_data = media_file.summary_data

                # facts for the roster/date header — generate_file_artifacts has
                # usually just run for the digest plane on this same file, so
                # its `source_fingerprint` short-circuit makes this call warm.
                row = generate_file_artifacts(db, file_id)
                facts = dict(row.facts or {}) if row is not None else {}
                # The stored summary may quote a speaker name the file's policy hides
                # while voiceprints are off: the INDEXED copy says the diarizer label
                # instead (#1280). The stored text is not rewritten.
                name_masks = hidden_speaker_names(db, file_id) if summary_data else {}

            documents = digest_mapping.build_summary_documents(
                file_uuid=file_uuid,
                file_id=file_id,
                summary_data=summary_data,
                facts=facts,
                base_metadata=base_metadata,
                name_masks=name_masks,
            )
            ids = digest_mapping.summary_document_ids(file_uuid, summary_data)
            written = 0
            if documents:
                written = self._bulk_index_documents(
                    list(zip(ids, documents, strict=True)), use_neural
                )
            # A file that LOST its summary (summary_data cleared, or shrank to
            # fewer leaves) must lose its documents too — same orphan hazard as
            # the digest tail (#400/#435).
            self._prune_stale_summary_leaves(file_uuid, keep_count=len(documents))

            if written < len(documents):
                logger.error(
                    f"Summary plane for file {file_uuid} is incomplete: {written} of "
                    f"{len(documents)} leaves landed"
                )
            return written
        except Exception as exc:  # noqa: BLE001 — chunks are indexed; do not fail the caller
            logger.error(f"Could not index summary plane for file {file_uuid}: {exc}")
            return 0

    def _prune_stale_summary_leaves(self, file_uuid: str, *, keep_count: int) -> int:
        """Delete summary leaves left behind by a longer previous summary.

        The same realtime `mget` gate as :meth:`_prune_stale_digests`, for the
        same reason (#435): a ``count`` gate would miss an orphan leaf exactly
        as often, for the identical reason — the bulk load above uses
        ``refresh=False``.
        """
        if not opensearch_client:
            return 0

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        try:
            stale_ids = self._orphaned_document_ids(
                index_name=index_name,
                document_id=lambda n: digest_mapping.summary_document_id(file_uuid, n),
                first_orphan=keep_count,
                plane_query=summary_plane_query(file_uuid),
                # NOT `chunk_index`: a summary's is a negative sentinel that
                # counts the wrong way. `summary_leaf_index` is what its
                # document id counts up.
                counter_field="summary_leaf_index",
            )
            if not stale_ids:
                return 0

            opensearch_client.indices.refresh(index=index_name)
            response = opensearch_client.delete_by_query(
                index=index_name,
                body={"query": summary_plane_query(file_uuid, from_leaf=keep_count)},
                refresh=True,
                conflicts="proceed",
            )
            deleted = int(response.get("deleted", 0))
            logger.info(f"Pruned {deleted} stale summary leaf/leaves for file {file_uuid}")
            if deleted != len(stale_ids):
                logger.warning(
                    f"Stale-summary prune for file {file_uuid} found {len(stale_ids)} "
                    f"orphan id(s) but the summary-plane predicate deleted {deleted}"
                )
            return deleted
        except Exception as e:  # noqa: BLE001 — logged; the new documents are already indexed
            logger.error(f"Could not prune stale summary leaves for file {file_uuid}: {e}")
            return 0
