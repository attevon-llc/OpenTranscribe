"""Neural ingest pipelines and the active-model setting.

Mixed into :class:`~app.services.search.ml_model_service.OpenSearchMLModelService`.
"""

import logging
from typing import Any

from app.services.search.ml_model_host import MLModelHost

logger = logging.getLogger(__name__)


class MLIngestPipelineMixin(MLModelHost):
    """Ingest-pipeline CRUD and active-model bookkeeping (mixed into ``OpenSearchMLModelService``)."""

    def create_ingest_pipeline(
        self,
        pipeline_id: str,
        model_id: str,
        source_field: str = "content",
        target_field: str = "embedding",
        description: str = "",
    ) -> bool:
        """Create a neural ingest pipeline with text_embedding processor.

        Args:
            pipeline_id: ID for the ingest pipeline.
            model_id: The deployed model ID to use.
            source_field: Field to read text from.
            target_field: Field to write embedding to.
            description: Optional pipeline description.

        Returns:
            True if pipeline was created successfully.
        """
        if not self._ensure_client():
            return False

        try:
            pipeline_body: dict[str, Any] = {
                "description": description or f"Neural embedding pipeline using model {model_id}",
                "processors": [
                    {
                        "text_embedding": {
                            "model_id": model_id,
                            "field_map": {source_field: target_field},
                        }
                    }
                ],
            }

            assert self._client is not None
            self._client.ingest.put_pipeline(id=pipeline_id, body=pipeline_body)
            logger.info(f"Created ingest pipeline: {pipeline_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to create ingest pipeline {pipeline_id}: {e}")
            return False

    def delete_ingest_pipeline(self, pipeline_id: str) -> bool:
        """Delete an ingest pipeline.

        Args:
            pipeline_id: ID of the pipeline to delete.

        Returns:
            True if deletion succeeded.
        """
        if not self._ensure_client():
            return False

        try:
            assert self._client is not None
            self._client.ingest.delete_pipeline(id=pipeline_id)
            logger.info(f"Deleted ingest pipeline: {pipeline_id}")
            return True

        except Exception as e:
            logger.debug(f"Could not delete pipeline {pipeline_id}: {e}")
            return False

    def get_ingest_pipeline(self, pipeline_id: str) -> dict[str, Any] | None:
        """Get an ingest pipeline configuration.

        Args:
            pipeline_id: ID of the pipeline.

        Returns:
            Pipeline configuration dict, or None if not found.
        """
        if not self._ensure_client():
            return None

        try:
            assert self._client is not None
            response = self._client.ingest.get_pipeline(id=pipeline_id)
            result: dict[str, Any] | None = response.get(pipeline_id)
            return result

        except Exception as e:
            logger.debug(f"Pipeline {pipeline_id} not found: {e}")
            return None

    def update_ingest_pipeline_model(self, pipeline_id: str, new_model_id: str) -> bool:
        """Update an existing ingest pipeline with a new model.

        Args:
            pipeline_id: ID of the pipeline to update.
            new_model_id: The new model ID to use.

        Returns:
            True if update succeeded.
        """
        current = self.get_ingest_pipeline(pipeline_id)
        if not current:
            logger.warning(f"Pipeline {pipeline_id} not found, cannot update")
            return False

        # Update the model_id in the text_embedding processor
        processors = current.get("processors", [])
        for processor in processors:
            if "text_embedding" in processor:
                processor["text_embedding"]["model_id"] = new_model_id

        return self.create_ingest_pipeline(
            pipeline_id=pipeline_id,
            model_id=new_model_id,
            description=current.get("description", ""),
        )

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
