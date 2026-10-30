"""Digest plane of ``TranscriptIndexingService``: regenerate, index and prune digest sections."""

import logging
from typing import Any

from app.core.config import settings
from app.services.ingest_artifacts import index_mapping as digest_mapping
from app.services.opensearch_service import opensearch_client

from ..plane_queries import digest_plane_query
from .bulk import BulkIndexMixin
from .orphans import OrphanProbeMixin

logger = logging.getLogger(__name__)


class DigestPlaneMixin(BulkIndexMixin, OrphanProbeMixin):
    """Per-file digest-section indexing and its stale-tail prune."""

    def _index_digest_plane(
        self,
        *,
        file_id: int,
        file_uuid: str,
        base_metadata: dict[str, Any],
        use_neural: bool,
    ) -> int:
        """Regenerate and index this file's digest documents.

        Regeneration happens **here**, on the per-file index path, for the
        reason addendum G1 gives: `delete_transcript_chunks` is unqualified and
        every rebuild trigger routes through this method's caller, so anything
        that is not regenerated here is destroyed. Stage 2's
        ``source_fingerprint`` short-circuit is what makes that affordable — an
        unchanged transcript costs a SHA-256, not a TextRank — and because the
        fingerprint covers the *resolved* speaker display name, a rename
        invalidates the row by itself and needs no separate trigger (#405).

        Its own session: the callers are a Celery task that has one open over a
        batch of files and an API path that has none, and borrowing the former's
        would make a digest failure roll back a whole batch's progress.

        An empty digest is a valid outcome (a ten-second clip has no sentence
        long enough), and a failure here is logged rather than raised: the
        chunks are already indexed and correct, and a caller must not report a
        good index as a failure.

        Args:
            file_id: Media file integer id.
            file_uuid: Media file UUID.
            base_metadata: Per-file fields shared with chunk documents. The ACL
                rewrite keys on ``file_id`` and the tenant backfill on
                ``file_uuid`` (addendum G5), so :func:`build_digest_documents`
                puts **both** on every digest — do not strip either.
            use_neural: Whether to run the documents through the ingest pipeline.

        Returns:
            Number of digest sections indexed.
        """
        try:
            from app.db.session_utils import session_scope
            from app.services.ingest_artifacts import generate_file_artifacts
            from app.services.ingest_artifacts import resolve_recorded_date_for_file

            with session_scope() as db:
                # **This is the back-catalogue backfill**, and it is here rather than in a
                # one-off script because it needs no re-ingest: the filename and the
                # transcript are already in Postgres for every existing row, so the first
                # reindex after this ships dates the whole library. Unlike the artifacts
                # below it has no fingerprint short-circuit, so an unchanged file is still
                # resolved — which is the entire point for rows that predate the column.
                #
                # Its own try: a date is an enrichment and the chunks are already indexed
                # and correct. A regex failure here must not cost the digest plane, and
                # the outer handler would report the whole digest index as failed.
                try:
                    resolve_recorded_date_for_file(db, file_id)
                except Exception as date_exc:  # noqa: BLE001 — enrichment, never fatal
                    logger.warning(
                        "Could not resolve recorded_date for file %s: %s", file_id, date_exc
                    )

                row = generate_file_artifacts(db, file_id)
                if row is None:
                    return 0
                digest = dict(row.digest or {})
                facts = dict(row.facts or {})

            documents = digest_mapping.build_digest_documents(
                file_uuid=file_uuid,
                file_id=file_id,
                digest=digest,
                facts=facts,
                base_metadata=base_metadata,
            )
            ids = digest_mapping.digest_document_ids(file_uuid, digest)
            written = 0
            if documents:
                written = self._bulk_index_documents(
                    list(zip(ids, documents, strict=True)), use_neural
                )
            # Same orphan hazard as #400's chunk tail: the ids embed the section
            # number, so a digest that re-sections shorter leaves the extras
            # behind, still matching every query the file matches.
            self._prune_stale_digests(file_uuid, keep_count=len(documents))

            # Report what LANDED, not what was built (issue #495). This used to
            # discard `_bulk_index_documents`'s return value entirely and return
            # `len(documents)`, so `digest_sections` in the task result was the number
            # of sections *generated* — reported identically whether all of them were
            # written or none were. `_bulk_index_documents` logs the failures and
            # returns a short count; nothing read it.
            #
            # Unlike the chunk plane above this does NOT raise, and the asymmetry is
            # deliberate: the `except` below already declines to fail the caller for a
            # digest problem, because the digest tier is derived enrichment while the
            # chunks are the transcript itself. A missing digest degrades summarization;
            # missing chunks make part of a recording unfindable. Reporting the true
            # count is what that decision needs to stay honest — "we chose not to fail"
            # is defensible, "we reported a number we did not verify" is not.
            if written < len(documents):
                logger.error(
                    f"Digest plane for file {file_uuid} is incomplete: {written} of "
                    f"{len(documents)} sections landed"
                )
            return written
        except Exception as exc:  # noqa: BLE001 — chunks are indexed; do not fail the caller
            logger.error(f"Could not index digest plane for file {file_uuid}: {exc}")
            return 0

    def _prune_stale_digests(self, file_uuid: str, *, keep_count: int) -> int:
        """Delete digest sections left behind by a longer previous sectioning.

        The same realtime gate as :meth:`_prune_stale_chunks`, for the same reason
        (#435) and with the same guarantee: this plane is written by the same
        unrefreshed bulk load, from the same five unserialised dispatch paths, so a
        count gate here misses an orphan section exactly as often. Fixing one plane
        and describing the race as closed would be a docstring asserting a guarantee
        the code does not provide — which is the defect #435 is about.
        """
        if not opensearch_client:
            return 0

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        try:
            stale_ids = self._orphaned_document_ids(
                index_name=index_name,
                document_id=lambda n: digest_mapping.digest_document_id(file_uuid, n),
                first_orphan=keep_count,
                plane_query=digest_plane_query(file_uuid),
                # NOT `chunk_index`: a digest's is a negative sentinel that counts
                # the wrong way. `digest_section` is what its document id counts up.
                counter_field="digest_section",
            )
            if not stale_ids:
                return 0

            opensearch_client.indices.refresh(index=index_name)
            response = opensearch_client.delete_by_query(
                index=index_name,
                body={"query": digest_plane_query(file_uuid, from_section=keep_count)},
                refresh=True,
                conflicts="proceed",
            )
            deleted = int(response.get("deleted", 0))
            logger.info(f"Pruned {deleted} stale digest section(s) for file {file_uuid}")
            if deleted != len(stale_ids):
                logger.warning(
                    f"Stale-digest prune for file {file_uuid} found {len(stale_ids)} "
                    f"orphan id(s) but the digest-plane predicate deleted {deleted}"
                )
            return deleted
        except Exception as e:  # noqa: BLE001 — logged; the new documents are already indexed
            logger.error(f"Could not prune stale digest sections for file {file_uuid}: {e}")
            return 0
