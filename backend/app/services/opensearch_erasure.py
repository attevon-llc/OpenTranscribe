"""Erase a file's or an account's OpenSearch documents, and prove they are gone.

One function per document family: speaker voiceprints, speaker-profile and cluster-centroid
embeddings (biometric data), the transcript document, the ``transcript_chunks`` RAG index
(both planes) and the legacy ``transcript_summaries`` index. Each reports through a
``fail(stage, err)`` callback instead of raising, so one failing store never stops the
others, and each verifies by counting survivors rather than trusting a delete helper that
answers ``False``/``0`` for both "absent" and "failed". :func:`_cleanup_opensearch_for_file`
runs the per-file set for ``file_cleanup_service.purge_media_file``;
``account_purge.purge_account_external_copies`` and ``speaker_cleanup`` call the
speaker-family erasers directly.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from opensearchpy import OpenSearch

logger = logging.getLogger(__name__)


def _opensearch_client() -> OpenSearch | None:
    """Return the OpenSearch client, or ``None`` when it is disabled/unbuilt."""
    from app.services.opensearch_service import opensearch_client

    client: OpenSearch | None = opensearch_client
    return client


def _count_surviving(index: str, query: dict[str, Any]) -> int:
    """Count documents still matching ``query`` in ``index`` after a delete.

    An **absent index answers 0** — the cluster confirmed the documents are
    gone. Anything else (no client, connection refused, auth failure) **raises**,
    because "I could not ask" is not "nothing is there"; the caller records that
    as a residual error rather than silently treating it as a clean sweep.

    Its caller is the retired summaries index (:func:`count_legacy_summary_docs`); the
    speaker family is verified across all its indices at once by
    :func:`_surviving_per_index`. **Do not route a
    ``transcript_chunks`` count through it**: since index v6 that index holds two
    kinds of document, so every read of it has to say which it means, and a
    helper that takes a caller-supplied predicate can only launder that decision
    (:meth:`TranscriptIndexingService.count_file_documents` is where it is made).

    Args:
        index: Index to count in.
        query: OpenSearch query body's ``query`` clause.

    Returns:
        The number of documents that survived the deletion attempt.

    Raises:
        Exception: The cluster could not be reached or could not answer.
    """
    from opensearchpy.exceptions import NotFoundError

    client = _opensearch_client()
    if client is None:
        raise RuntimeError("OpenSearch client unavailable")
    try:
        if not client.indices.exists(index=index):
            return 0
        # The deletes this verifies are by id, which is realtime; ``count`` reads a
        # searcher, which keeps seeing a deleted document until the next refresh. Without
        # this, every purge that landed inside the refresh window reported its (already
        # deleted) voiceprints as survivors and audited a clean erasure as PARTIAL.
        client.indices.refresh(index=index)
        return int(client.count(index=index, body={"query": query})["count"])
    except NotFoundError:
        return 0


def _cleanup_opensearch_for_file(target: dict[str, Any], file_uuid: str) -> list[dict[str, Any]]:
    """Remove all OpenSearch documents associated with a media file.

    Covers: speaker embeddings (v3 + v4 + alias), the transcript document, the
    ``transcript_chunks`` RAG index, and transcript summaries.

    Takes **plain data, never an ORM instance**: it used to read ``file.speakers``
    and ``file.id`` off a live row, which lazy-loads — i.e. reopens a transaction
    — in the middle of half a dozen OpenSearch round trips. The speaker UUIDs are
    now enumerated in :func:`_load_purge_plan`'s short read session instead.

    Best-effort means **one failing store never stops the others** — it does NOT
    mean failures are invisible. Every step that could not prove its documents
    are gone is returned to the caller. This used to wrap each step in
    ``contextlib.suppress(Exception)``, so an OpenSearch outage during a GDPR
    Art. 17 erasure destroyed the DB rows (and the account) while leaving the
    verbatim transcript and its RAG chunks indexed and searchable — reported to
    the caller, the API and the audit log as a completed erasure, with the row
    that would identify what to re-delete already gone.

    A delete's own answer is not proof: a bulk delete reports per item and
    ``delete_transcript_chunks`` raises on failure but cannot see documents inside
    the refresh window. The surviving-document count is the evidence.

    Args:
        target: The purge plan from :func:`_load_purge_plan` — ``file_id``,
            ``speaker_uuids`` and an optional ``speaker_read_error``.
        file_uuid: The file's UUID, used as the transcript/chunk document key.

    Returns:
        One ``{"stage", "file_uuid", "error"}`` dict per store whose documents
        may have survived. Empty means every store confirmed them gone.
    """
    residual: list[dict[str, Any]] = []

    def _fail(stage: str, err: object) -> None:
        logger.warning(f"OpenSearch erasure incomplete for file {file_uuid} at '{stage}': {err}")
        residual.append({"stage": stage, "file_uuid": file_uuid, "error": str(err)})

    # A read failure in the DB phase is still a miss: the embeddings it would
    # have named are unaccounted for, so it must surface as a residual here.
    read_error = target.get("speaker_read_error")
    if read_error:
        _fail("speakers", f"could not enumerate the file's speakers: {read_error}")

    _erase_speaker_docs(list(target.get("speaker_uuids") or []), _fail)
    _erase_transcript_doc(file_uuid, _fail)
    _erase_transcript_chunks(file_uuid, _fail)
    _erase_summary_docs(int(target["file_id"]), _fail)
    return residual


def _erase_speaker_docs(speaker_uuids: list[str], fail: Callable[[str, object], None]) -> None:
    """Delete speaker voiceprints (biometric data) from every speaker index and verify.

    Documents are keyed by the speaker's UUID. See :func:`_erase_speaker_family_docs` for
    the three requests this costs, whatever the number of speakers or indices.
    """
    _erase_speaker_family_docs(list(speaker_uuids), "speakers", "voiceprint", fail)


def _erase_profile_docs(profile_uuids: list[str], fail: Callable[[str, object], None]) -> None:
    """Delete speaker-profile embeddings (``profile_<uuid>``, biometric data) and verify.

    Used by :func:`purge_account_external_copies` after the account's rows have committed
    (issue #715), and by ``speaker_cleanup`` when a profile is deleted or its embedding can
    no longer be rebuilt (issue #1214). A mid-migration profile can sit in an index the
    main-index helpers do not address (the legacy v3 or its backup), which is why every
    speaker index is targeted.
    """
    doc_ids = [f"profile_{u}" for u in profile_uuids]
    _erase_speaker_family_docs(doc_ids, "profiles", "profile embedding", fail)


def _erase_cluster_docs(cluster_uuids: list[str], fail: Callable[[str, object], None]) -> None:
    """Delete speaker-cluster centroid documents (``cluster_<uuid>``) and verify.

    A centroid is the mean of the cluster's member voiceprints — biometric data in its
    own right — and a mid-migration cluster can sit in v3 or v4.
    """
    doc_ids = [f"cluster_{u}" for u in cluster_uuids]
    _erase_speaker_family_docs(doc_ids, "clusters", "centroid", fail)


def _erase_speaker_family_docs(
    doc_ids: list[str], stage: str, label: str, fail: Callable[[str, object], None]
) -> None:
    """Delete ``doc_ids`` from every speaker index, then prove none survive. Three requests.

    1. One ``_bulk`` request holding a ``delete`` action per (index, id). A by-id delete is
       realtime, so it also removes a document written inside the refresh window, which a
       ``delete_by_query`` (it reads a searcher) would not see. A delete never auto-creates
       its index; an absent index or document answers that item 404, the desired end
       state. Other item errors are logged; the survivor count below decides.
    2. One ``_refresh`` over every speaker index, so the count reads a current searcher.
    3. One search over every speaker index with a ``terms`` aggregation on ``_index``: the
       survivor count, per concrete index, for the residual.

    This replaced a per-id helper call plus, per index, an existence check, a
    ``delete_by_query?refresh=true``, a second existence check, a refresh and a count —
    about 20 sequential requests on the speaker-merge request path under load.

    Docs: OpenSearch Bulk API (delete action, per-item ``status`` / ``error``), Refresh
    index API and Count/Search multi-target ``<index>,<index>`` with
    ``ignore_unavailable``, and the ``_index`` metadata field ("count results per index"
    with a terms aggregation).
    """
    if not doc_ids:
        return
    from app.services.opensearch_service.speaker_maintenance import speaker_embedding_indices

    indices = speaker_embedding_indices()
    try:
        client = _require_client()
        actions = [
            {"delete": {"_index": idx, "_id": doc_id}} for idx in indices for doc_id in doc_ids
        ]
        _log_refused_deletes(client.bulk(body=actions), label)
    except Exception as e:  # any failure is a residual; the count still runs
        logger.exception("Erasure: %s delete request failed", label)
        fail(stage, e)

    try:
        for idx, left in _surviving_per_index(indices, {"ids": {"values": doc_ids}}).items():
            fail(stage, f"{left} {label} doc(s) survive in {idx}")
    except Exception as e:  # unverifiable == not proven gone
        logger.exception("Erasure: could not verify %s removal in %s", label, indices)
        fail(stage, f"could not verify {', '.join(indices)}: {e}")


def _require_client() -> OpenSearch:
    client = _opensearch_client()
    if client is None:
        raise RuntimeError("OpenSearch client unavailable")
    return client


def _log_refused_deletes(response: dict[str, Any], label: str) -> None:
    """Log every bulk delete the cluster refused for a reason other than "not there"."""
    if not response.get("errors"):
        return
    refused = [
        item.get("delete") or {}
        for item in response.get("items") or []
        if int((item.get("delete") or {}).get("status", 200)) not in (200, 404)
    ]
    for item in refused:
        logger.warning(
            "Erasure: %s delete refused in %s (status %s, %s)",
            label,
            item.get("_index"),
            item.get("status"),
            (item.get("error") or {}).get("type"),
        )


def _surviving_per_index(indices: list[str], query: dict[str, Any]) -> dict[str, int]:
    """Documents still matching ``query``, keyed by concrete index; absent indices are 0.

    One refresh and one search across every target (an alias resolves to its concrete
    index, counted once). Anything that prevents an answer raises: "I could not ask" is
    not "nothing is there".
    """
    client = _require_client()
    targets = ",".join(indices)
    client.indices.refresh(index=targets, ignore_unavailable=True)
    response = client.search(
        index=targets,
        ignore_unavailable=True,
        body={
            "size": 0,
            "query": query,
            "aggs": {"per_index": {"terms": {"field": "_index", "size": len(indices)}}},
        },
    )
    buckets = response["aggregations"]["per_index"]["buckets"]
    return {str(b["key"]): int(b["doc_count"]) for b in buckets if int(b["doc_count"])}


def _erase_transcript_doc(file_uuid: str, fail: Callable[[str, object], None]) -> None:
    """Delete the transcript document — the verbatim text of the recording."""
    try:
        from opensearchpy.exceptions import NotFoundError

        from app.services.opensearch_service import settings as os_settings

        client = _opensearch_client()
        if client is None:
            raise RuntimeError("OpenSearch client unavailable")
        # NotFoundError is the cluster ANSWERING that the document is not there,
        # which is the desired end state — not a failure to remove it.
        with contextlib.suppress(NotFoundError):
            client.delete(index=os_settings.OPENSEARCH_TRANSCRIPT_INDEX, id=file_uuid)
    except Exception as e:
        logger.exception("Erasure: transcript document delete failed for file %s", file_uuid)
        fail("transcript", e)


def _erase_transcript_chunks(file_uuid: str, fail: Callable[[str, object], None]) -> None:
    """Delete the file's documents from the RAG index, and verify none survive.

    A failed delete is recorded even when the count then reads 0, and only the
    count proves a delete that returned. This index stores transcript text
    UNREDACTED, so a survivor here is the raw content.

    **Both of the index's planes are meant here, not the chunk plane** (index v6,
    #403 Stage 3). ``transcript_chunks`` also holds per-file ``doc_type: digest``
    documents whose sentences are verbatim transcript text, and
    ``delete_transcript_chunks`` removes both by design. A chunk-plane survivor
    count would therefore report a clean sweep while the digest of an erased
    recording stayed indexed — the erasure reporting success while transcript
    text survives, which is the exact shape this function's error reporting was
    written for.

    Neither the delete nor the count builds its predicate here: both are
    ``file_plane_query``, in ``search/plane_queries``, which owns the plane
    vocabulary. Building a second predicate at this call site is what would let
    the check and the thing it checks drift apart.
    """
    try:
        from app.services.search.indexing_service import TranscriptIndexingService

        TranscriptIndexingService().delete_transcript_chunks(file_uuid)
    except Exception as e:  # any failure is a residual; the count still runs
        logger.exception("Erasure: transcript chunk delete failed for file %s", file_uuid)
        # The typed outage carries a fixed message; the residual keeps the cluster's own reason.
        fail("transcript_chunks", e.__cause__ or e)

    try:
        from app.services.search.indexing_service import TranscriptIndexingService

        left = TranscriptIndexingService().count_file_documents(file_uuid)
        if left:
            fail("transcript_chunks", f"{left} document(s) survive")
    except Exception as e:  # unverifiable == not proven gone
        logger.exception(
            "Erasure: could not verify transcript chunk removal for file %s", file_uuid
        )
        fail("transcript_chunks", f"could not verify: {e}")


def legacy_summary_index_name() -> str:
    """Physical name of the retired ``transcript_summaries`` index (#67).

    Upgraded deployments may still hold it with real user summaries, so the erasure paths keep
    sweeping it and the erasure receipt keeps measuring it. This module is where the retired name
    is allowed to live; everything else asks here.
    """
    from app.services.opensearch_service import settings as os_settings

    return str(os_settings.OPENSEARCH_SUMMARY_INDEX)


def count_legacy_summary_docs(file_ids: list[int]) -> int:
    """Documents of ``file_ids`` still in the retired summaries index. An absent index is 0."""
    if not file_ids:
        return 0
    return _count_surviving(legacy_summary_index_name(), {"terms": {"file_id": file_ids}})


def _erase_summary_docs(file_id: int, fail: Callable[[str, object], None]) -> None:
    """Delete the file's LLM-written summaries — prose about the recording."""
    try:
        from app.services.opensearch_service import settings as os_settings

        client = _opensearch_client()
        if client is None:
            raise RuntimeError("OpenSearch client unavailable")
        summary_index = os_settings.OPENSEARCH_SUMMARY_INDEX
        if not client.indices.exists(index=summary_index):
            return
        resp = client.delete_by_query(
            index=summary_index,
            body={"query": {"term": {"file_id": file_id}}},
            refresh=True,
        )
        # delete_by_query reports per-document failures in the BODY rather than
        # raising, so a partial sweep looks like a success without this.
        failures = (resp or {}).get("failures") or []
        if failures:
            fail("transcript_summaries", f"delete_by_query reported {len(failures)} failure(s)")
    except Exception as e:
        logger.exception("Erasure: summary index delete failed for file %s", file_id)
        fail("transcript_summaries", e)
