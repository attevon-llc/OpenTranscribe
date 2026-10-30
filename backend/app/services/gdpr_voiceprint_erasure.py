"""GDPR erasure of voiceprint (biometric) documents in the OpenSearch speaker indices.

The speaker and profile embeddings are not reachable by a Postgres ``ON DELETE CASCADE``,
so ``gdpr_erasure_service`` deletes them here, and the privacy purge
(``services/privacy/purge_voiceprints.py`` and ``purge_residual.py``) reuses the same
index enumeration to delete and then measure by document id. Nothing here raises: every
failure is appended to the caller's ``errors`` list so the erasure audits PARTIAL.
"""

import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)


def _erase_speaker_voiceprints(
    *,
    user_id: int | None = None,
    organization_id: int | None = None,
    personal_only: bool = False,
    errors: list[dict[str, Any]] | None = None,
) -> int:
    """Delete speaker/profile embedding docs (biometric data) from OpenSearch.

    Scopes by ``user_id`` and/or ``organization_id`` (both given = the
    intersection, i.e. one member's docs within one org) and runs a
    ``delete_by_query`` across the v3 + v4 + alias speaker indices **and the
    legacy ``speakers_v3_backup``**. The backup index is not optional
    housekeeping: ``opensearch_service/indices._restore_v3_from_backup``
    reindexes it into ``speakers_v3`` whenever v3 is found empty, so on a small
    deployment erasing its last subject, the next ``ensure_indices_exist()``
    put their biometric embeddings straight back. Legacy installs only — the
    index does not exist on a fresh one, and ``indices.exists`` skips it.
    Best-effort: a down/absent OpenSearch never blocks the relational +
    storage erasure (those are the legally-binding deletions; this catches
    orphaned biometric docs).

    Args:
        user_id: Scope the deletion to one user.
        organization_id: Scope the deletion to one organization.
        personal_only: Also require that the doc carries no organization, i.e. only
            personal-workspace docs (the privacy purge's "personal" scope). Without a
            ``user_id`` this is every user's personal docs. Contradicts
            ``organization_id``: giving both deletes nothing.
        errors: The caller's ``summary["errors"]`` list. Failures are appended
            here so the erasure is audited as PARTIAL rather than SUCCESS —
            biometric data surviving an Art. 17 request must not be recorded as
            a completed erasure.

    Returns the number of voiceprint documents deleted (0 if OpenSearch is
    unavailable).
    """
    if user_id is None and organization_id is None and not personal_only:
        return 0
    if personal_only and organization_id is not None:
        return 0

    terms: list[dict[str, Any]] = []
    if user_id is not None:
        terms.append({"term": {"user_id": user_id}})
    if organization_id is not None:
        terms.append({"term": {"organization_id": organization_id}})
    body: dict[str, Any] = {"query": {"bool": {"filter": terms}}}
    if personal_only:
        body["query"]["bool"]["must_not"] = [{"exists": {"field": "organization_id"}}]

    label = (
        "org " + str(organization_id)
        if organization_id
        else ("user " + str(user_id) if user_id else "personal scope")
    )
    return delete_speaker_docs_by_query(
        body, errors=errors, user_id=user_id, organization_id=organization_id, label=label
    )


def delete_speaker_docs_by_query(
    body: dict[str, Any],
    *,
    errors: list[dict[str, Any]] | None = None,
    user_id: int | None = None,
    organization_id: int | None = None,
    label: str = "",
) -> int:
    """Run ``delete_by_query(body)`` over every speaker index (v3, v4, alias, legacy backup).

    The one place the index set is enumerated. ``_erase_speaker_voiceprints`` builds the scope
    from a user and/or organization; the privacy purge (``services/privacy/purge.py``) also
    needs "everything", which is why the query is a parameter. Failures go to ``errors`` (see
    ``_erase_speaker_voiceprints``); this never raises.

    Returns:
        The number of documents deleted (0 if OpenSearch is unavailable).
    """

    def _record(reason: str) -> None:
        if errors is not None:
            errors.append(
                {
                    "stage": "voiceprints",
                    "user_id": user_id,
                    "org_id": organization_id,
                    "error": reason,
                }
            )

    deleted = 0
    try:
        from app.core.embedding_models import get_speaker_index
        from app.core.embedding_models import get_speaker_index_v3
        from app.core.embedding_models import get_speaker_index_v3_backup
        from app.core.embedding_models import get_speaker_index_v4
        from app.services.opensearch_service import opensearch_client

        if opensearch_client is None:
            logger.warning("OpenSearch unavailable — skipping voiceprint erasure (orphan docs)")
            _record("OpenSearch client unavailable")
            return 0

        indices = {
            get_speaker_index(),
            get_speaker_index_v3(),
            get_speaker_index_v4(),
            get_speaker_index_v3_backup(),
        }
        for idx in indices:
            try:
                if not opensearch_client.indices.exists(index=idx):
                    continue
                resp = opensearch_client.delete_by_query(
                    index=idx,
                    body=body,
                    refresh=True,
                    conflicts="proceed",
                )
                deleted += int(resp.get("deleted", 0))
                # delete_by_query reports per-document failures in the BODY
                # rather than raising, so a partial sweep of biometric docs
                # would otherwise look identical to a complete one.
                failures = (resp or {}).get("failures") or []
                if failures:
                    _record(f"index {idx}: {len(failures)} document(s) failed to delete")
            except Exception as idx_err:  # noqa: BLE001 — best-effort per index
                logger.warning(f"Voiceprint erasure failed on index {idx}: {idx_err}")
                _record(f"index {idx}: {idx_err}")
    except Exception as e:  # noqa: BLE001 — OpenSearch optional
        logger.warning(f"Voiceprint erasure skipped (OpenSearch error): {e}")
        _record(str(e))

    if deleted:
        logger.info(f"Erased {deleted} voiceprint doc(s) for {label or 'a scope'}")
    return deleted


_DOC_ID_BATCH = 1000


def _speaker_index_names(client: Any) -> list[str]:
    """The speaker indices that exist (v3, v4, alias, legacy backup), without duplicates."""
    from app.core.embedding_models import get_speaker_index
    from app.core.embedding_models import get_speaker_index_v3
    from app.core.embedding_models import get_speaker_index_v3_backup
    from app.core.embedding_models import get_speaker_index_v4

    wanted = [
        get_speaker_index(),
        get_speaker_index_v3(),
        get_speaker_index_v4(),
        get_speaker_index_v3_backup(),
    ]
    return [idx for idx in dict.fromkeys(wanted) if client.indices.exists(index=idx)]


def delete_speaker_docs_by_ids(
    doc_ids: Iterable[str],
    *,
    also: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    label: str = "",
) -> int:
    """Delete speaker-index documents by ``_id`` across every speaker index.

    The privacy purge selects voiceprint documents by the ids Postgres says belong to its scope
    (speaker uuids, ``profile_<uuid>``, ``cluster_<uuid>``), never by the documents' own tenant
    stamp: a document written before the tenant backfill carries no stamp, and a stamp-based
    delete misses it in an organization purge and can reach it from a member's personal one.
    Failures go to ``errors`` and this never raises.

    Args:
        doc_ids: The ``_id`` values to delete.
        also: A query clause for documents to delete IN ADDITION to the ids (an organization
            purge passes the organization's positive stamp, which reaches documents Postgres
            has no row for). Run after the ids, so a document matching both counts once.
        errors: The caller's error list.
        label: For the log line.

    Returns:
        What OpenSearch reports as deleted (0 if it is unavailable), never the number of ids
        asked for.
    """
    ids = list(dict.fromkeys(str(i) for i in doc_ids))
    deleted = 0
    for start in range(0, len(ids), _DOC_ID_BATCH):
        batch = ids[start : start + _DOC_ID_BATCH]
        deleted += delete_speaker_docs_by_query(
            {"query": {"ids": {"values": batch}}}, errors=errors, label=label
        )
    if also is not None:
        deleted += delete_speaker_docs_by_query({"query": also}, errors=errors, label=label)
    return deleted


def count_speaker_docs_by_ids(
    doc_ids: Iterable[str],
    *,
    also: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> int | None:
    """How many of these ids still exist as speaker-index documents, after a refresh.

    The residual check of a purge. The indices are listed in ONE request so an alias and the
    index behind it count a document once. ``None`` means it could not be measured (recorded in
    ``errors``): an unmeasurable store must never read as an empty one.

    ``also`` is the clause :func:`delete_speaker_docs_by_ids` was given: documents matching it
    are counted too, once (an id that also matches it is not counted twice).
    """
    ids = list(dict.fromkeys(str(i) for i in doc_ids))
    if not ids and also is None:
        return 0
    try:
        from app.services.opensearch_service import opensearch_client

        if opensearch_client is None:
            raise RuntimeError("OpenSearch client unavailable")
        indices = _speaker_index_names(opensearch_client)
        if not indices:
            return 0
        target = ",".join(indices)
        opensearch_client.indices.refresh(index=target)
        found = 0
        if also is not None:
            found += int(
                opensearch_client.count(index=target, body={"query": also}).get("count", 0)
            )
        for start in range(0, len(ids), _DOC_ID_BATCH):
            batch = ids[start : start + _DOC_ID_BATCH]
            by_id: dict[str, Any] = {"ids": {"values": batch}}
            query = {"bool": {"filter": [by_id], "must_not": [also]}} if also else by_id
            found += int(
                opensearch_client.count(index=target, body={"query": query}).get("count", 0)
            )
        return found
    except Exception as exc:  # noqa: BLE001 - reported, and the caller treats it as unmeasured
        logger.warning(f"Voiceprint residual count failed: {exc}")
        if errors is not None:
            errors.append({"stage": "residual_count", "error": str(exc)})
        return None


def _erase_profile_embedding(profile_uuid: str, errors: list[dict[str, Any]]) -> None:
    """Remove one speaker profile's embedding doc, recording a failure. Never raises.

    This used to be ``contextlib.suppress(Exception)`` at all three call sites, which is
    the same defect already fixed for the transcript document: a biometric embedding
    that could not be deleted made the erasure report SUCCESS, and the DB row naming
    which document to retry was destroyed in the same pass. Best-effort is right;
    silent is not.
    """
    try:
        from app.services.opensearch_service import remove_profile_embedding

        remove_profile_embedding(profile_uuid)
    except Exception as e:  # noqa: BLE001 — record, never abort the relational delete
        logger.warning(f"Profile embedding removal failed for {profile_uuid}: {e}")
        errors.append({"stage": "profile_embedding", "profile_uuid": profile_uuid, "error": str(e)})
