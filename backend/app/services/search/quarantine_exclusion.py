"""The quarantine (takedown) exclusion set for search read surfaces, failing closed.

Quarantine is Postgres-only; OpenSearch documents carry no field for it, so a search
surface that builds results from the index itself applies this set as a ``must_not``:
autocomplete and facets (``suggestions``), the corpus-wide match count
(``HybridSearchService.count_matches``) and, via ``summary_search``, the summary plane.
``hybrid_search_service`` binds :func:`_quarantined_file_uuids` too; ``summary_search`` and
the search endpoints import it from there.
"""

import logging

logger = logging.getLogger(__name__)


# A single OpenSearch `terms` clause is bounded by `index.max_terms_count`
# (65536 by default); quarantine is expected to be rare, so this cap is a
# defensive ceiling, not a normal operating limit. It is queried with `+ 1` so
# hitting it is DETECTABLE — a silently truncated exclusion set is the same
# failure as no exclusion set at all, just harder to notice (issue #876).
_QUARANTINED_UUID_CAP = 10_000


class QuarantineExclusionUnavailableError(RuntimeError):
    """The set of quarantined files could not be resolved completely.

    Raised instead of degrading to a partial (or empty) exclusion set. Callers
    must treat it as "I cannot prove taken-down content is absent from this
    response" and withhold the response, mirroring how the redaction plane
    withholds snippet text rather than emitting text it could not mask.
    """


class SearchCountUnavailableError(QuarantineExclusionUnavailableError):
    """``count_matches`` could not produce a trustworthy count.

    A subclass of :class:`QuarantineExclusionUnavailableError`, not a sibling —
    ``search.py``'s single ``except QuarantineExclusionUnavailableError`` must
    catch both "the quarantine exclusion set could not be resolved" and "the
    OpenSearch query itself failed", because both mean the same thing to the
    caller: this number cannot be trusted, and reporting it as ``0`` is a
    content oracle (issue #817) — a caller polling this endpoint while
    searching for a term learns whether it exists somewhere they cannot see.
    """


def _quarantined_file_uuids() -> list[str]:
    """Every currently-quarantined file's uuid, for excluding facets built from it.

    Quarantine (``takedown_service.quarantine_file``) is Postgres-only — it never
    writes to OpenSearch — so a facet-aggregation query has no field of its own to
    filter on and must resolve the exclusion set here first. Not scoped to a
    caller: it feeds a ``must_not`` against a query already scoped to what that
    caller can see (``accessible_user_ids``), so a global list only ever narrows
    that intersection, never widens what a caller could learn.

    **Fails closed** (issue #876). This is the mechanism that keeps taken-down
    content out of a read surface, not an approximate sidebar count, so neither
    of its two failure modes may degrade quietly:

    * a DB error used to return ``[]``, which means *exclude nothing* — a
      transient outage served quarantined files' facets with an HTTP 200 and an
      exception in a log nobody watches;
    * exceeding ``_QUARANTINED_UUID_CAP`` used to truncate with no signal at all.

    Returns:
        Quarantined file uuids as strings.

    Raises:
        QuarantineExclusionUnavailableError: The set could not be read, or is
            larger than ``_QUARANTINED_UUID_CAP`` so it cannot be expressed as a
            single ``terms`` clause without dropping members.
    """
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile

    try:
        with session_scope() as db:
            rows = (
                db.query(MediaFile.uuid)
                .filter(MediaFile.is_quarantined.is_(True))
                .order_by(MediaFile.quarantined_at.desc().nullslast())
                # One more than the cap: the extra row is what makes truncation
                # distinguishable from "there are exactly `cap` of them".
                .limit(_QUARANTINED_UUID_CAP + 1)
                .all()
            )
    except Exception as exc:
        logger.exception("Could not resolve quarantined file uuids for facet exclusion")
        raise QuarantineExclusionUnavailableError(
            "The quarantine exclusion set could not be read; "
            "search filters are unavailable until it can be."
        ) from exc

    if len(rows) > _QUARANTINED_UUID_CAP:
        logger.error(
            "More than %d quarantined files: the facet exclusion set cannot be "
            "expressed without dropping members",
            _QUARANTINED_UUID_CAP,
        )
        raise QuarantineExclusionUnavailableError(
            f"More than {_QUARANTINED_UUID_CAP} files are quarantined; the facet "
            "exclusion set cannot be applied completely."
        )

    return [str(row[0]) for row in rows]
