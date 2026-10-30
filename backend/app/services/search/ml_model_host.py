"""Attributes and cross-method calls the ML-model mixins rely on.

The mixins (``ml_model_registration``, ``ml_model_deployment``, ``ml_ingest_pipelines``) are mixed
into :class:`~app.services.search.ml_model_service.OpenSearchMLModelService`, which owns the
client (``__init__`` / ``_ensure_client``) and the status/listing reads. Every mixin reaches
OpenSearch through ``self._ensure_client()`` and never imports ``get_opensearch_client`` itself,
so a test patching ``ml_model_service.get_opensearch_client`` still takes effect everywhere.
Declaring the contract here lets the type checker verify each mixin; nothing in this class
exists at runtime beyond the annotations.
"""

from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from opensearchpy import OpenSearch


class MLModelHost:
    """Contract a mixin may assume of the ``OpenSearchMLModelService`` it is mixed into."""

    _client: "OpenSearch | None"
    _ml_settings_configured: bool

    if TYPE_CHECKING:

        def _ensure_client(self) -> bool: ...

        def configure_ml_settings(self) -> bool: ...

        def get_model_status(self, model_id: str) -> dict[str, Any]: ...

        def list_models(self, deployed_only: bool = False) -> list[dict[str, Any]]: ...

        def find_model_by_name(self, model_name: str) -> str | None: ...
