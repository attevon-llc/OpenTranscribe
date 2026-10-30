"""Bulk indexing and transient-failure retry for the chunks index."""

import contextlib
import logging
import secrets
import time
from collections.abc import Iterator
from typing import Any

from app.core.config import settings
from app.services.opensearch_service import opensearch_client
from app.services.opensearch_service.client import call_idempotent_write

logger = logging.getLogger(__name__)

# Transient bulk error types that are safe to retry
_RETRYABLE_ERROR_TYPES = frozenset(
    {
        "es_rejected_execution_exception",
        "circuit_breaking_exception",
        "cluster_block_exception",
    }
)

#: Retry budget for the transient bulk errors above (#495 follow-on).
#:
#: This was 2 attempts at 1 s and 2 s, and it was too short for every condition the
#: set above names. MEASURED: the ML Commons breaker
#: (``circuit_breaking_exception: Memory Circuit Breaker is open``) failed **8 of 8
#: documents twice through the full 1 s + 2 s budget** — because it trips on
#: *instantaneous* JVM heap-used, which under bulk load is dominated by uncollected
#: young-generation garbage and stays high until G1 runs. The same node measured
#: ``old gen 382 MB`` of a 4 GB heap: a false trip, not exhaustion.
#:
#: `ml_model_service.configure_ml_settings` raises the threshold that caused it, so
#: this is the second line of defence rather than the fix. It still matters: a
#: rejected-execution or a closed index (both in the set above) also outlast three
#: seconds, and the alternative is failing the task.
#:
#: Jitter is not decoration. Every worker indexing when a shared cluster resource
#: goes over its limit retries on the same schedule, so a fixed backoff has them all
#: return together and re-trip it — the retry becomes the load.
_BULK_RETRY_ATTEMPTS = 4
_BULK_RETRY_BASE_SECONDS = 1.0
_BULK_RETRY_MAX_SECONDS = 8.0

# Permanent error types that should NOT be retried
_PERMANENT_ERROR_TYPES = frozenset(
    {
        "mapper_parsing_exception",
        "strict_dynamic_mapping_exception",
        "illegal_argument_exception",
    }
)


@contextlib.contextmanager
def _suspended_refresh_for_large_index(
    index_name: str,
    *,
    chunk_count: int,
    threshold: int,
) -> Iterator[None]:
    """Suspend an index's ``refresh_interval`` while a bulk load runs.

    Only triggers when ``chunk_count >= threshold`` — small transcripts
    still benefit from near-real-time search. On exit the prior interval
    is restored and a manual refresh is issued so the newly loaded
    chunks become searchable immediately.

    Args:
        index_name: Target OpenSearch index.
        chunk_count: Number of chunks about to be loaded.
        threshold: Minimum chunk count that activates the suspension.
    """
    if chunk_count < threshold or not opensearch_client:
        yield
        return

    prior_interval: str | None = None
    try:
        settings_resp = opensearch_client.indices.get_settings(index=index_name)
        prior_interval = (
            settings_resp.get(index_name, {})
            .get("settings", {})
            .get("index", {})
            .get("refresh_interval")
        )
    except Exception as e:
        logger.debug(f"Could not read refresh_interval for {index_name}, skipping suspension: {e}")
        yield
        return

    try:
        opensearch_client.indices.put_settings(
            index=index_name, body={"index": {"refresh_interval": "-1"}}
        )
        logger.info(
            f"Suspended refresh on {index_name} for bulk load of {chunk_count} chunks "
            f"(prior refresh_interval={prior_interval!r})"
        )
        yield
    finally:
        try:
            restored = prior_interval if prior_interval is not None else "1s"
            opensearch_client.indices.put_settings(
                index=index_name, body={"index": {"refresh_interval": restored}}
            )
            opensearch_client.indices.refresh(index=index_name)
            logger.info(f"Restored refresh_interval={restored!r} on {index_name} and refreshed")
        except Exception as e:
            logger.warning(
                f"Failed to restore refresh_interval on {index_name}: {e}. "
                f'Run `PUT /{index_name}/_settings -d \'{{"index":{{"refresh_interval":"1s"}}}}\'`'
            )


class BulkIndexMixin:
    """Bulk write paths: chunk batches, id-addressed documents, and per-item retry."""

    def _bulk_index_documents(
        self, documents: list[tuple[str, dict[str, Any]]], use_neural_pipeline: bool
    ) -> int:
        """Bulk index ``(_id, document)`` pairs. Used by the digest plane.

        Separate from :meth:`_bulk_index_chunks` because that one derives the id
        from ``chunk_index``, and a digest's id is ``{uuid}_digest_{n}`` —
        ``{uuid}_0`` is already chunk 0.
        """
        if not opensearch_client or not documents:
            return 0

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        bulk_body: list[Any] = []
        for doc_id, document in documents:
            action: dict[str, Any] = {"index": {"_index": index_name, "_id": doc_id}}
            if use_neural_pipeline:
                action["index"]["pipeline"] = settings.OPENSEARCH_NEURAL_PIPELINE
            bulk_body.append(action)
            bulk_body.append(document)

        response = call_idempotent_write(opensearch_client.bulk, body=bulk_body, refresh=False)
        if response.get("errors"):
            failed = [
                item["index"]
                for item in response.get("items", [])
                if item.get("index", {}).get("error")
            ]
            logger.error(f"Digest bulk index reported {len(failed)} failure(s): {failed[:3]}")
            return len(documents) - len(failed)
        return len(documents)

    def _bulk_index_chunks(
        self, chunks: list[dict[str, Any]], use_neural_pipeline: bool = False
    ) -> int:
        """Bulk index chunks to OpenSearch in batches.

        Splits chunks into batches of SEARCH_BULK_BATCH_SIZE to avoid
        timeouts on large files. Failed documents with transient errors
        are retried with exponential backoff.

        Args:
            chunks: List of chunk documents to index.
            use_neural_pipeline: If True, use neural ingest pipeline for embedding.

        Returns:
            Number of successfully indexed chunks.
        """
        if not opensearch_client:
            raise RuntimeError("OpenSearch client not initialized")

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        batch_size = settings.SEARCH_BULK_BATCH_SIZE
        total_indexed = 0

        for batch_start in range(0, len(chunks), batch_size):
            batch = chunks[batch_start : batch_start + batch_size]
            bulk_body: list[Any] = []

            for chunk in batch:
                doc_id = f"{chunk['file_uuid']}_{chunk['chunk_index']}"
                index_action: dict[str, Any] = {
                    "index": {
                        "_index": index_name,
                        "_id": doc_id,
                    }
                }

                # Use neural ingest pipeline if enabled
                if use_neural_pipeline:
                    index_action["index"]["pipeline"] = settings.OPENSEARCH_NEURAL_PIPELINE

                bulk_body.append(index_action)
                bulk_body.append(chunk)

            response = call_idempotent_write(opensearch_client.bulk, body=bulk_body, refresh=False)

            if response.get("errors"):
                failed_docs = self._extract_failed_docs(response, batch)
                succeeded = len(batch) - len(failed_docs)
                total_indexed += succeeded

                if failed_docs:
                    retried = self._retry_failed_docs(failed_docs, index_name, use_neural_pipeline)
                    total_indexed += retried
            else:
                total_indexed += len(batch)

            if len(chunks) > batch_size:
                logger.debug(
                    f"Bulk batch {batch_start // batch_size + 1}: "
                    f"indexed {len(batch)} chunks (offset {batch_start})"
                )

        return total_indexed

    def _extract_failed_docs(
        self, response: dict[str, Any], batch: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Extract documents that failed with transient errors from a bulk response.

        Permanent errors (e.g. mapping exceptions) are logged and skipped.
        Transient errors (e.g. circuit breaker, rejected execution) are returned
        for retry.

        Args:
            response: OpenSearch bulk response.
            batch: The original batch of chunk documents.

        Returns:
            List of chunk documents that should be retried.
        """
        failed_docs: list[dict[str, Any]] = []
        permanent_count = 0

        for i, item in enumerate(response.get("items", [])):
            index_result = item.get("index", {})
            error_info = index_result.get("error")
            if not error_info:
                continue

            error_type = error_info.get("type", "")
            error_reason = error_info.get("reason", "")

            if error_type in _PERMANENT_ERROR_TYPES:
                # Permanent error -- log and skip
                permanent_count += 1
                logger.error(f"Permanent bulk index error (skipping): {error_type}: {error_reason}")
            elif error_type in _RETRYABLE_ERROR_TYPES:
                # Known transient error -- eligible for retry
                if i < len(batch):
                    failed_docs.append(batch[i])
                logger.warning(
                    f"Transient bulk index error (will retry): {error_type}: {error_reason}"
                )
            else:
                # Unknown error type -- log and skip (don't blindly retry)
                permanent_count += 1
                logger.error(f"Unknown bulk index error (skipping): {error_type}: {error_reason}")

        if permanent_count:
            logger.error(f"Bulk indexing had {permanent_count} permanent errors (not retried)")
        if failed_docs:
            logger.info(f"Bulk indexing has {len(failed_docs)} transient failures to retry")

        return failed_docs

    def _retry_failed_docs(
        self,
        failed_docs: list[dict[str, Any]],
        index_name: str,
        use_neural: bool,
        max_retries: int = _BULK_RETRY_ATTEMPTS,
    ) -> int:
        """Retry per-item bulk failures with backoff (call_idempotent_write re-sends a timed-out call).

        Args:
            failed_docs: List of chunk documents to retry.
            index_name: OpenSearch index name.
            use_neural: Whether to use the neural ingest pipeline.
            max_retries: Maximum number of retry attempts.

        Returns:
            Number of successfully indexed documents after retries.
        """
        if not opensearch_client or not failed_docs:
            return 0

        retried_count = 0
        remaining = list(failed_docs)

        for attempt in range(1, max_retries + 1):
            if not remaining:
                break

            # Exponential with jitter, capped: ~1s, 2s, 4s, 8s (+/- 25%).
            # `secrets` rather than `random` because ruff's S311 is right that the
            # stdlib PRNG is the wrong default, and the jitter only needs to
            # decorrelate concurrent workers — which a CSPRNG does equally well, at a
            # cost measured in microseconds, at most 4 times per failed batch. That is
            # cheaper than justifying a suppression.
            backoff = min(_BULK_RETRY_BASE_SECONDS * (2 ** (attempt - 1)), _BULK_RETRY_MAX_SECONDS)
            backoff *= 0.75 + (secrets.randbelow(1000) / 2000.0)
            logger.info(
                f"Retrying {len(remaining)} failed docs (attempt {attempt}/{max_retries}, "
                f"backoff {backoff:.1f}s)"
            )
            time.sleep(backoff)

            bulk_body: list[Any] = []
            for chunk in remaining:
                doc_id = f"{chunk['file_uuid']}_{chunk['chunk_index']}"
                index_action: dict[str, Any] = {
                    "index": {
                        "_index": index_name,
                        "_id": doc_id,
                    }
                }
                if use_neural:
                    index_action["index"]["pipeline"] = settings.OPENSEARCH_NEURAL_PIPELINE
                bulk_body.append(index_action)
                bulk_body.append(chunk)

            try:
                response = call_idempotent_write(
                    opensearch_client.bulk, body=bulk_body, refresh=False
                )
            except Exception as e:
                logger.error(f"Retry attempt {attempt} bulk call failed: {e}")
                continue

            if not response.get("errors"):
                retried_count += len(remaining)
                remaining = []
                break

            # Check which ones still failed
            still_failed: list[dict[str, Any]] = []
            for i, item in enumerate(response.get("items", [])):
                index_result = item.get("index", {})
                if index_result.get("error"):
                    if i < len(remaining):
                        still_failed.append(remaining[i])
                else:
                    retried_count += 1

            remaining = still_failed

        if remaining:
            logger.error(f"{len(remaining)} documents failed after {max_retries} retries")

        if retried_count:
            logger.info(f"Successfully retried {retried_count} documents")

        return retried_count
