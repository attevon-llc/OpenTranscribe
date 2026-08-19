"""The settings UI must be able to tell a multilingual model from an English one (#453).

``GET /search/models`` — the endpoint the **settings UI** reads, and therefore the only one
an admin sees before switching — returned `model_id`, `name`, `dimension`, `description` and
`size_mb`, and dropped `languages`/`language_type`. The ops endpoint has returned both all
along, so the two views of the same registry disagreed for no reason, and a multilingual
model was identifiable only by reading the words "Multilingual" out of its display name.

That is a real decision being made blind: switching the embedding model re-embeds **every
user's** corpus, and a multilingual model typically costs a few points on English. An admin
has to be able to see which kind they are choosing.

The registry is the single source: these assertions read `OPENSEARCH_EMBEDDING_MODELS` and
require the endpoint to agree with it, rather than restating a model list that would rot the
first time one is added.
"""

from __future__ import annotations

from app.core.constants import OPENSEARCH_EMBEDDING_MODELS


def test_every_model_reports_its_language_type(client, auth_headers) -> None:
    """The defect: the settings UI had no field to key a badge off."""
    response = client.get("/api/search/models", headers=auth_headers)

    assert response.status_code == 200, response.text
    models = response.json()["models"]
    assert models, "no models returned; every assertion below would pass vacuously"

    for model in models:
        assert "language_type" in model, (
            f"{model['model_id']} carries no language_type, so the settings UI cannot "
            "distinguish a multilingual model from an English-only one"
        )
        assert "languages" in model, f"{model['model_id']} carries no languages list"


def test_the_payload_agrees_with_the_registry(client, auth_headers) -> None:
    """Two sources for one fact drift. The endpoint must not paraphrase the registry."""
    response = client.get("/api/search/models", headers=auth_headers)

    assert response.status_code == 200, response.text
    by_id = {m["model_id"]: m for m in response.json()["models"]}

    assert set(by_id) == set(OPENSEARCH_EMBEDDING_MODELS), (
        "the endpoint and the registry disagree about which models exist"
    )
    for model_id, info in OPENSEARCH_EMBEDDING_MODELS.items():
        assert by_id[model_id]["language_type"] == info["language_type"]
        assert by_id[model_id]["languages"] == info["languages"]


def test_at_least_one_model_of_each_kind_is_offered(client, auth_headers) -> None:
    """Guard the guard: a badge is only meaningful if both values actually occur.

    If the registry ever held only English models, every assertion above would still pass
    while the badge silently became a constant.
    """
    response = client.get("/api/search/models", headers=auth_headers)

    assert response.status_code == 200, response.text
    kinds = {m["language_type"] for m in response.json()["models"]}

    assert "multilingual" in kinds and "english" in kinds, (
        f"expected both kinds of model to be offered, saw {sorted(kinds)}"
    )
