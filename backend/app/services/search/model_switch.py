"""Point search at a different embedding model — completely (issue #437).

**The one implementation of the switch.** It used to be two halves in two
endpoints, and neither half was a switch:

- ``POST /search/models`` (the settings UI) wrote the ``search.embedding_model``
  and ``search.embedding_dimension`` settings and dispatched a reindex, but never
  touched the ML active model or the ingest pipeline. **No vector changed and the
  setting simply lied** — and not inertly: the reindex coordinator honours that
  dimension, and ``recreate_index_for_dimension`` *deletes the chunks index* when
  it disagrees with the mapping.
- ``PUT /search/models/neural/active`` (ops) repointed the pipeline and recreated
  the index but never wrote those settings, so the coordinator then read the
  **old** dimension and recreated the index a second time at the wrong size. A
  dimension-changing switch was broken outright, not merely mixed.

Ordering is load-bearing — settings, pipeline, caches, index, reindex — and the
reindex reaches **every owner**, not the caller. See each function's docstring.
"""

from __future__ import annotations

import logging
from typing import Any
from typing import cast

from app.core.constants import OPENSEARCH_EMBEDDING_MODELS
from app.core.exceptions import SearchIndexError

logger = logging.getLogger(__name__)


class UnknownEmbeddingModelError(SearchIndexError):
    """The requested model is not in ``OPENSEARCH_EMBEDDING_MODELS``."""


class EmbeddingModelNotDeployedError(SearchIndexError):
    """The requested model is not registered *and* deployed in OpenSearch.

    Raised **before anything is persisted**. Recording a selection for a model
    that cannot embed anything is what made the legacy path destructive rather
    than merely wrong: the reindex coordinator honours the new dimension, deletes
    the whole chunks index, and then fails every write because the untouched
    pipeline still emits the previous model's vector width.
    """


class ReindexDispatchError(SearchIndexError):
    """The re-index could not be queued.

    Unlike the two above, this can be raised **after** the settings, the ingest
    pipeline and the index mapping have already been changed — the deployment is
    then genuinely half-switched, and that state exists whether or not anyone is
    told. The only decision left is whether to *report* it, and reporting it is
    not optional: a switch whose reindex never ran leaves new documents embedded
    by the new model and every existing document by the old one, which is
    precisely the mixed vector space #437 exists to prevent. Swallowing this
    would answer ``200`` with "Re-indexing the transcripts of 0 user(s)".

    ``apply_embedding_model_switch`` re-raises it with that switch-specific
    framing; the plain ``POST /search/reindex`` path (#627) reports the generic
    message this module raises, because nothing was switched there.
    """


def dispatch_reindex_for_every_owner(
    triggered_by: int,
    file_uuids_by_owner: dict[int, list[str]] | None = None,
) -> dict[str, Any]:
    """Reindex **every** user's transcripts, not just the caller's.

    ``reindex_transcripts_task`` filters ``MediaFile.user_id == user_id``
    (``tasks/reindex_task.py``), so the "full reindex" both switch endpoints used
    to dispatch covered only the admin who pressed the button. On any multi-user
    deployment that leaves every other user's chunks holding vectors from the
    previous model, permanently and silently, in the same kNN space as the freshly
    embedded ones — and it is not a race or a rare interleaving, it is what the
    documented happy path does. ``reindex_task._check_and_recreate_stale_index``
    already names this hazard for the index-*version* case; this is the same
    hazard on the model axis.

    **The dimension must be persisted BEFORE this is called.** Each coordinator
    reconciles the index against ``get_search_embedding_dimension()``, and
    ``recreate_index_for_dimension`` *deletes the index* when they disagree. With
    the setting already correct every coordinator sees a match and no-ops, which
    is what makes fanning out safe; with a stale setting, N coordinators would
    race to delete the index out from under each other's workers. Concurrent
    coordinators are not a new shape either —
    ``tasks/search_maintenance_task._dispatch_reindex_tasks`` already loops one
    per user with unindexed files.

    **A failed dispatch is not caught per user, and that is deliberate.** The
    obvious shape here is a ``try``/``except`` around each ``.delay()`` so one
    user's failure cannot sink the other twenty. Measured, there is no such
    failure: the only argument that varies between the calls is an ``int``
    (``file_uuids`` is ``None`` for every user), so nothing can fail for user 7
    and succeed for user 8. Every real failure mode — broker unreachable, result
    backend unreachable, queue rejection — is global, and a per-call catch would
    turn "nothing was queued" into a ``200`` reporting *0 users re-indexed*
    against a pipeline that has already been repointed. That is the silent
    divergence this whole issue is about, so it is raised instead.

    (Measured rather than assumed, because the narrower catch is not obvious:
    ``.delay()`` against a dead broker raises a plain ``builtins.RuntimeError`` —
    "Retry limit exceeded while trying to reconnect to the Celery result store
    backend" — with an empty ``__cause__`` chain. It is **not** a
    ``kombu.exceptions.OperationalError`` and **not** an ``OSError``, so catching
    those by name would have caught nothing while looking careful.)

    **A partial scope names its owners explicitly** (#627). ``POST
    /search/reindex`` has two narrower modes — a pending-only sweep and an
    operator-supplied list of file UUIDs — and both were scoped to the calling
    admin by the same defect this function fixes for the whole-corpus case. They
    pass ``file_uuids_by_owner`` so each owner's coordinator receives that
    owner's files, resolved by ``services/search/reindex_scope.py``. There is
    deliberately no second dispatch loop for them: one loop, one failure
    contract, one place that decides the caller goes first.

    ⚠️ **An owner with an empty list is DROPPED, not dispatched.**
    ``reindex_transcripts_task`` treats a falsy ``file_uuids`` as "every file
    this user owns" (``if file_uuids:``), so passing ``[]`` for an owner with
    nothing to do would re-embed their entire account.

    Args:
        triggered_by: The admin performing the switch or repair, dispatched
            first so their own progress stream starts immediately.
        file_uuids_by_owner: Optional per-owner file scope. ``None`` (the
            default) means the whole corpus: every owner of a COMPLETED file,
            each re-indexed in full.

    Returns:
        Dict with the dispatched task ids keyed by user id, and a user count.

    Raises:
        ReindexDispatchError: no reindex could be queued.
    """
    from app.db.session_utils import session_scope
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.tasks.reindex_task import reindex_transcripts_task

    payloads: dict[int, list[str] | None]
    if file_uuids_by_owner is None:
        with session_scope() as db_session:
            owner_ids = [
                int(row[0])
                for row in db_session.query(MediaFile.user_id)
                .filter(MediaFile.status == FileStatus.COMPLETED)
                .distinct()
                .all()
            ]
        # The caller is dispatched first and unconditionally: the settings UI keys
        # its progress stream on the requesting user, so an admin who owns no files
        # still needs a run to watch. That costs one no-op coordinator.
        payloads = {uid: None for uid in owner_ids}
        payloads[triggered_by] = None
    else:
        payloads = {uid: list(uuids) for uid, uuids in file_uuids_by_owner.items() if uuids}

    ordered = ([triggered_by] if triggered_by in payloads else []) + [
        uid for uid in sorted(payloads) if uid != triggered_by
    ]
    tasks: dict[int, str] = {}
    try:
        for user_id in ordered:
            tasks[user_id] = reindex_transcripts_task.delay(
                user_id=user_id, file_uuids=payloads[user_id]
            ).id
    except Exception as e:
        raise ReindexDispatchError(
            f"The re-index could not be queued after {len(tasks)} of {len(ordered)} "
            f"users ({e}). Restore the message broker and re-run it."
        ) from e

    logger.info(f"Dispatched reindex for {len(tasks)} users")
    return {"reindex_task_ids": tasks, "reindex_users": len(tasks)}


def apply_embedding_model_switch(model_name: str, triggered_by: int) -> dict[str, Any]:
    """Switch the embedding model and re-embed everything that used the old one.

    Args:
        model_name: A key of ``OPENSEARCH_EMBEDDING_MODELS``.
        triggered_by: User id performing the switch.

    Returns:
        Dict describing what was applied, including the post-switch provenance
        survey.

    Raises:
        UnknownEmbeddingModelError: ``model_name`` is not in the registry.
        EmbeddingModelNotDeployedError: it is, but OpenSearch cannot embed with it.
    """
    if model_name not in OPENSEARCH_EMBEDDING_MODELS:
        raise UnknownEmbeddingModelError(f"Unknown model: {model_name}")

    from app.services.search.embedding_provenance import reset_search_provenance_advisory
    from app.services.search.embedding_provenance import survey_embedding_models
    from app.services.search.hybrid_search_service import clear_search_cache
    from app.services.search.hybrid_search_service import reset_neural_search_state
    from app.services.search.indexing_service import ensure_neural_ingest_pipeline
    from app.services.search.indexing_service import recreate_index_for_dimension
    from app.services.search.indexing_service import reset_neural_pipeline_state
    from app.services.search.ml_model_service import get_ml_model_service
    from app.services.search.settings_service import save_search_embedding_model

    model_info = OPENSEARCH_EMBEDDING_MODELS[model_name]
    dimension = cast(int, model_info["dimension"])

    ml_service = get_ml_model_service()
    model_id = ml_service.find_model_by_name(model_name)
    if not model_id or not ml_service.get_model_status(model_id).get("deployed"):
        raise EmbeddingModelNotDeployedError(
            f"{model_name} is not registered and deployed in OpenSearch, so it "
            f"cannot embed anything. Register and deploy it first "
            f"(POST /search/models/neural/{model_name}/register then /deploy), then "
            f"switch. Recording the selection without it would change the index "
            f"dimension while the pipeline kept emitting the old model's vectors."
        )

    # 1. Settings. Both keys, together — `dispatch_reindex_for_every_owner`
    #    explains why the dimension has to be right before any coordinator runs.
    save_search_embedding_model(model_name, dimension)

    # 2. The pipeline, which is what actually decides the vectors — and what the
    #    #437 provenance label is resolved from.
    ml_service.set_active_model_id(model_id)
    reset_neural_pipeline_state()
    ensure_neural_ingest_pipeline(model_id)

    # 3. Caches, then the index mapping.
    clear_search_cache()
    reset_neural_search_state()
    # The search response's provenance advisory is TTL-cached (#437); a model
    # switch is exactly the event that invalidates it, so drop it here rather than
    # letting a stale "all comparable" survive the switch for another minute.
    reset_search_provenance_advisory()
    recreate_index_for_dimension(dimension)

    # 4. Everyone's documents, not just the caller's. The dispatcher's own
    #    failure message is scope-generic, so the switch-specific consequence —
    #    a half-applied switch leaves a MIXED vector space — is stated here,
    #    where it is true, rather than in a helper two other callers share.
    try:
        dispatch = dispatch_reindex_for_every_owner(triggered_by)
    except ReindexDispatchError as e:
        raise ReindexDispatchError(
            f"The embedding model was switched, but the re-embed could not be queued: "
            f"{e} New documents will be embedded by the new model and existing ones "
            f"still hold the old model's vectors — the index is a mixed vector space "
            f"until a full reindex runs."
        ) from e

    logger.info(f"Embedding model switched to {model_name} ({model_id}, {dimension}d)")
    return {
        "model_name": model_name,
        # The ML Commons id is deliberately NOT called `model_id` here: the older
        # `POST /search/models` contract used that name for the HuggingFace model
        # name, and the two are different strings for the same switch.
        "ml_model_id": model_id,
        "dimension": dimension,
        "provenance": provenance_payload(survey_embedding_models()),
        **dispatch,
    }


def provenance_payload(survey: Any) -> dict[str, Any]:
    """Serialize an :class:`~.embedding_provenance.EmbeddingProvenance` for the wire.

    Args:
        survey: The survey to render.

    Returns:
        A JSON-safe dict. ``message`` is the operator-facing sentence; the rest is
        what a script would branch on.
    """
    return {
        "verdict": survey.verdict,
        "mixed": survey.mixed,
        "comparable": survey.comparable,
        "models": list(survey.known_models),
        "counts": survey.counts,
        "total": survey.total,
        "unattributed": survey.unattributed,
        "no_embedding": survey.no_embedding,
        "message": survey.describe(),
    }
