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


# ---------------------------------------------------------------------------
# Readiness: the picker has to be able to see that a model cannot be applied
# ---------------------------------------------------------------------------
def test_every_model_reports_whether_it_can_actually_embed(client, auth_headers) -> None:
    """Without this the settings UI offered every model as if it were usable.

    Choosing one that had never been downloaded answered **409 with instructions to
    POST two API endpoints by hand**, which is not a user interface. The 409 itself is
    correct and stays (#437) — recording a selection whose pipeline cannot emit the
    new dimension makes the coordinator delete the chunks index and then fail every
    write. What was missing was any way to see the condition coming.
    """
    response = client.get("/api/search/models", headers=auth_headers)

    assert response.status_code == 200, response.text
    models = response.json()["models"]
    assert models, "no models returned; the assertion below would pass vacuously"
    for model in models:
        assert isinstance(model.get("ready"), bool), (
            f"{model['model_id']} has no boolean `ready`, so the picker cannot tell a "
            "usable model from one that has never been downloaded"
        )


def test_readiness_costs_one_cluster_call_for_the_whole_list(
    client, auth_headers, monkeypatch
) -> None:
    """This runs on every settings page load; per-model lookups would be N round trips.

    The first draft of the endpoint called the resolver *inside* the list
    comprehension, which is exactly that mistake.
    """
    calls: list[bool] = []

    # NOTE the `self`: patching a CLASS attribute means the instance arrives as the
    # first positional argument. Without it the call raises TypeError, the
    # endpoint's except swallows it, and BOTH tests below pass for the wrong
    # reason — 0 calls and every model reported not-ready.
    def _one_call(self, deployed_only: bool = False) -> list[dict]:  # noqa: ANN001
        calls.append(deployed_only)
        return []

    monkeypatch.setattr(
        "app.services.search.ml_model_service.OpenSearchMLModelService.list_models",
        _one_call,
    )

    response = client.get("/api/search/models", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert len(calls) == 1, (
        f"the deployed-model lookup ran {len(calls)} times for "
        f"{len(response.json()['models'])} models; it must be resolved once"
    )
    assert calls == [True], "readiness must ask only for DEPLOYED models"


def test_an_unreachable_cluster_reports_nothing_as_ready(client, auth_headers, monkeypatch) -> None:
    """Fail closed: claiming readiness we cannot confirm ends in a deleted index.

    The switch would refuse the model anyway, so an optimistic `ready` would only
    produce a button that 409s — and the failure it hides is the destructive one.
    """

    def _boom(self, deployed_only: bool = False) -> list[dict]:  # noqa: ANN001
        raise RuntimeError("cluster unreachable")

    monkeypatch.setattr(
        "app.services.search.ml_model_service.OpenSearchMLModelService.list_models",
        _boom,
    )

    response = client.get("/api/search/models", headers=auth_headers)

    assert response.status_code == 200, "a readiness probe must not break the picker"
    assert all(m["ready"] is False for m in response.json()["models"])
