"""The active embedding model setting.

Mixed into :class:`~app.services.search.ml_model_service.OpenSearchMLModelService`.
"""

import logging

from app.services.search.ml_model_host import MLModelHost

logger = logging.getLogger(__name__)


class MLIngestPipelineMixin(MLModelHost):
    """Active-model bookkeeping (mixed into ``OpenSearchMLModelService``).

    The ingest pipeline itself is owned by ``search.pipelines.ensure_neural_ingest_pipeline``.
    """

    def get_active_model_id(self) -> str | None:
        """Get the currently active model ID from settings.

        Returns the model_id stored in the database settings, or adopts the one
        deployed model as a fallback when the stored one is gone.

        **The fallback refuses to choose between candidates** (issue #437). It
        used to return ``list_models(deployed_only=True)[0]`` — the first hit of a
        ``match_all`` search with no sort, i.e. an arbitrary model. The caller
        that matters is
        :func:`~app.services.search.pipelines.ensure_neural_ingest_pipeline`,
        which writes whatever it is handed into the ingest pipeline; the drift
        check then sees the model change and recreates the pipeline. So an
        arbitrary pick silently repoints embedding at a different model **with no
        user action at all**, and every document indexed afterwards lands in the
        same kNN space as documents from the previous one. Nothing about the
        result looks wrong.

        Exactly one deployed model is not a choice, and adopting it is what
        recovers neural search after the stored id goes stale (an OpenSearch
        volume lost and the model re-registered under a new id — the case this
        fallback was written for). It is still logged, because it is still a
        change of model, and the resulting documents are now labelled with it.
        More than one deployed model **is** a choice, and the right move is to
        make the operator make it: returning ``None`` degrades search to BM25,
        which is loud, obvious and reversible, where the wrong guess is silent
        and costs a full reindex.

        Returns:
            Model ID string, or None when there is no unambiguous active model.
        """
        # First check database/settings for configured model
        from app.services.search.settings_service import _get_setting

        stored_model_id = _get_setting("search.opensearch_model_id")
        if stored_model_id:
            # Verify it's actually deployed
            status = self.get_model_status(stored_model_id)
            if status.get("deployed"):
                return stored_model_id
            logger.warning(f"Stored model {stored_model_id} is not deployed")

        deployed = self.list_models(deployed_only=True)
        if len(deployed) == 1:
            adopted = deployed[0].get("model_id")
            logger.warning(
                f"No active embedding model is configured; adopting the only deployed "
                f"model {adopted} ({deployed[0].get('name')}). If that is not the model "
                f"the existing index was built with, its vectors are not comparable and "
                f"a full reindex is required."
            )
            return adopted
        if len(deployed) > 1:
            names = ", ".join(f"{m.get('name')} ({m.get('model_id')})" for m in deployed)
            logger.error(
                f"No active embedding model is configured and {len(deployed)} are "
                f"deployed, so there is no unambiguous choice: {names}. Neural search "
                f"stays OFF until one is set via PUT /search/models/neural/active — "
                f"picking one here would silently embed new documents with a different "
                f"model than the index already holds."
            )

        return None

    def set_active_model_id(self, model_id: str) -> None:
        """Persist the active model ID to database settings.

        Args:
            model_id: The model ID to set as active.
        """
        from app.services.search.settings_service import _set_setting

        _set_setting(
            "search.opensearch_model_id",
            model_id,
            "OpenSearch ML model ID for neural search",
        )
        logger.info(f"Set active OpenSearch model: {model_id}")
