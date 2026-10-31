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

import contextlib
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _opensearch_client():
    """Return the OpenSearch client, or ``None`` when it is disabled/unbuilt."""
    from app.services.opensearch_service import opensearch_client

    return opensearch_client


def _count_surviving(index: str, query: dict[str, Any]) -> int:
    """Count documents still matching ``query`` in ``index`` after a delete.

    An **absent index answers 0** — the cluster confirmed the documents are
    gone. Anything else (no client, connection refused, auth failure) **raises**,
    because "I could not ask" is not "nothing is there"; the caller records that
    as a residual error rather than silently treating it as a clean sweep.

    Its callers are the voiceprint and cluster-centroid sweeps, which address
    documents by id across speaker indices that have no planes. **Do not route a
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

    Two of the steps go through helpers that swallow their own errors and return
    a value that cannot distinguish "absent" from "failed"
    (``remove_speaker_embedding`` returns ``False`` for both;
    ``delete_transcript_chunks`` returns ``0`` for both). For those, the
    surviving-document count — not the helper's return value — is the evidence.

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
    """Delete the file's speaker embeddings (biometric data) and verify.

    ``remove_speaker_embedding`` already sweeps every index in
    ``speaker_embedding_indices()`` (v3, v4, the alias, the legacy v3 backup), so there is
    exactly one deletion path — but it swallows its own errors and returns
    ``False`` for "absent" and "failed" alike, so the surviving-document count is
    what actually proves the embeddings are gone.
    """
    if not speaker_uuids:
        return

    for speaker_uuid in speaker_uuids:
        try:
            from app.services.opensearch_service import remove_speaker_embedding

            remove_speaker_embedding(speaker_uuid)
        except Exception as e:  # noqa: BLE001 — it swallows its own; this is belt-and-braces
            fail("speakers", e)

    from app.services.opensearch_service.speaker_maintenance import speaker_embedding_indices

    for idx in speaker_embedding_indices():
        try:
            left = _count_surviving(idx, {"ids": {"values": speaker_uuids}})
            if left:
                fail("speakers", f"{left} voiceprint doc(s) survive in {idx}")
        except Exception as e:  # noqa: BLE001 — unverifiable == not proven gone
            fail("speakers", f"could not verify {idx}: {e}")


def _erase_profile_docs(profile_uuids: list[str], fail: Callable[[str, object], None]) -> None:
    """Delete the account's speaker-profile embeddings (biometric data).

    Sibling of :func:`_erase_speaker_docs` for :class:`SpeakerProfile` rows (issue
    #715) — used by :func:`purge_account_external_copies` after the account's rows have
    committed, and by ``speaker_cleanup`` when a profile is deleted or its embedding can
    no longer be rebuilt (issue #1214). ``remove_profile_embedding`` sweeps its own main +
    v4-staging indices and swallows its own errors (returns ``False`` for both "absent"
    and "failed"), so, as for voiceprints, a survivor count over every speaker index is
    what proves the ``profile_<uuid>`` documents are gone.
    """
    if not profile_uuids:
        return

    for profile_uuid in profile_uuids:
        try:
            from app.services.opensearch_service import remove_profile_embedding

            remove_profile_embedding(profile_uuid)
        except Exception as e:  # noqa: BLE001 — it swallows its own; this is belt-and-braces
            fail("profiles", e)

    from app.services.opensearch_service.speaker_maintenance import speaker_embedding_indices

    # A mid-migration profile can sit in an index ``remove_profile_embedding`` does not
    # address (the legacy v3 or its backup), so every speaker index is swept by id.
    # ``conflicts="proceed"``: the by-id delete above is realtime but the sweep's scroll
    # reads a searcher, which can still list the document it just deleted; the 409 that
    # produces means "already gone", the end state. The survivor count is the proof.
    query = {"ids": {"values": [f"profile_{u}" for u in profile_uuids]}}
    for idx in speaker_embedding_indices():
        try:
            client = _opensearch_client()
            if client is None:
                raise RuntimeError("OpenSearch client unavailable")
            if client.indices.exists(index=idx):
                client.delete_by_query(
                    index=idx, body={"query": query}, refresh=True, conflicts="proceed"
                )
            left = _count_surviving(idx, query)
            if left:
                fail("profiles", f"{left} profile embedding doc(s) survive in {idx}")
        except Exception as e:  # noqa: BLE001 — unverifiable == not proven gone
            fail("profiles", f"could not verify {idx}: {e}")


def _erase_cluster_docs(cluster_uuids: list[str], fail: Callable[[str, object], None]) -> None:
    """Delete speaker-cluster centroid documents (``cluster_<uuid>``) and verify.

    A centroid is the mean of the cluster's member voiceprints — biometric data in its
    own right. Swept across every speaker index (a mid-migration cluster can sit in v3
    or v4), and, like the voiceprints, proven gone by a survivor count rather than by
    the delete's own return value.
    """
    if not cluster_uuids:
        return
    from app.services.opensearch_service.speaker_maintenance import speaker_embedding_indices

    query = {"ids": {"values": [f"cluster_{u}" for u in cluster_uuids]}}
    for idx in speaker_embedding_indices():
        try:
            client = _opensearch_client()
            if client is None:
                raise RuntimeError("OpenSearch client unavailable")
            if client.indices.exists(index=idx):
                client.delete_by_query(index=idx, body={"query": query}, refresh=True)
            left = _count_surviving(idx, query)
            if left:
                fail("clusters", f"{left} centroid doc(s) survive in {idx}")
        except Exception as e:  # noqa: BLE001 — unverifiable == not proven gone
            fail("clusters", f"could not erase/verify {idx}: {e}")


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
    except Exception as e:  # noqa: BLE001
        fail("transcript", e)


def _erase_transcript_chunks(file_uuid: str, fail: Callable[[str, object], None]) -> None:
    """Delete the file's documents from the RAG index, and verify none survive.

    ``delete_transcript_chunks`` returns 0 for "no chunks", "index absent" AND
    "the delete failed", so only the count is evidence. This index stores
    transcript text UNREDACTED, so a survivor here is the raw content.

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
    except Exception as e:  # noqa: BLE001 — it swallows its own; this is belt-and-braces
        fail("transcript_chunks", e)

    try:
        from app.services.search.indexing_service import TranscriptIndexingService

        left = TranscriptIndexingService().count_file_documents(file_uuid)
        if left:
            fail("transcript_chunks", f"{left} document(s) survive")
    except Exception as e:  # noqa: BLE001 — unverifiable == not proven gone
        fail("transcript_chunks", f"could not verify: {e}")


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
    except Exception as e:  # noqa: BLE001
        fail("transcript_summaries", e)
