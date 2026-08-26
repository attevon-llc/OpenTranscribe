"""Functional tests for the neural-search model lifecycle routes (audit B9).

``endpoints/search.py`` carries seven routes ``scripts/audit-route-coverage.py``
found exercised by no test: ``GET /filters``, ``GET/PUT /models/neural/status``
(only the GET half — ``active`` is PUT-only, see below), ``POST
.../{model_name:path}/{register,deploy,undeploy}`` and ``POST
/repair-indices``. ``test_search_admin_routes.py`` already covers the sibling
``GET/POST /models`` (the legacy dimension-picker pair) and ``/reindex*``; this
file is deliberately scoped to the ones it does not touch.

**Nothing here reaches a real OpenSearch cluster or a real HuggingFace
download.** ``get_ml_model_service`` is a small recording stand-in for the four
register/deploy/undeploy/status/find calls, same shape as
``_StandInMLService`` in the sibling file. ``PUT .../active`` triggers a full
per-owner reindex (``services/search/model_switch.py``); this file tests only
its request-acceptance/validation contract (400 unknown model, 409 not
registered+deployed) and never lets a real switch happen — the 409 case is
reached without touching Postgres or OpenSearch at all, because
``apply_embedding_model_switch`` raises before writing anything.

The path-wildcard test matters because ``model_name`` is typed
``{model_name:path}`` and flows into ``ml_model_service`` calls keyed by name
match, not string interpolation into a URL: confirmed by reading
``ml_model_service.py`` — ``find_model_by_name`` runs an exact-match scan over
``list_models()``, and ``register_neural_model`` 400s anything outside the
static ``OPENSEARCH_EMBEDDING_MODELS`` registry before it reaches the service
at all. A traversal-shaped name should therefore surface as an ordinary
"not registered"/"unknown model" refusal, never a 500 or a request that
reached OpenSearch with the raw string spliced into a path.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import status

from app.core.constants import OPENSEARCH_EMBEDDING_MODELS

_BASE = "/api/search"
_KNOWN_MODEL = next(iter(OPENSEARCH_EMBEDDING_MODELS))


class _StandInMLService:
    """Records register/deploy/undeploy/find/status calls; no OpenSearch reached."""

    def __init__(self, deployed: bool = False, registered: bool = True):
        self.deployed = deployed
        self.registered = registered
        self.calls: list[tuple[str, str]] = []

    def find_model_by_name(self, model_name: str) -> str | None:
        self.calls.append(("find_model_by_name", model_name))
        if not self.registered:
            return None
        return "stand-in-model-id"

    def get_model_status(self, model_id: str) -> dict[str, object]:
        self.calls.append(("get_model_status", model_id))
        return {"deployed": self.deployed}

    def register_model(self, model_name: str, **_kwargs: object) -> str | None:
        self.calls.append(("register_model", model_name))
        return "stand-in-model-id"

    def deploy_model(self, model_id: str) -> bool:
        self.calls.append(("deploy_model", model_id))
        self.deployed = True
        return True

    def undeploy_model(self, model_id: str) -> bool:
        self.calls.append(("undeploy_model", model_id))
        self.deployed = False
        return True

    def list_models(self, deployed_only: bool = False) -> list[dict[str, object]]:
        return []

    def get_active_model_id(self) -> str | None:
        return None


@contextmanager
def _standin_ml_service(**kwargs: object):
    service = _StandInMLService(**kwargs)  # type: ignore[arg-type]
    with patch("app.services.search.ml_model_service.get_ml_model_service", return_value=service):
        yield service


# ---------------------------------------------------------------------------
# GET /filters
# ---------------------------------------------------------------------------


def test_filters_requires_authentication(client):
    assert client.get(f"{_BASE}/filters").status_code == status.HTTP_401_UNAUTHORIZED


def test_filters_degrades_gracefully_without_an_opensearch_client(client, user_token_headers):
    """Confirmed in ``hybrid_search_service.get_available_filters``: no client
    means an empty-but-200 payload, not a 500 — a facet panel with nothing
    selected must still render the rest of the search page."""
    with patch("app.services.search.hybrid_search_service.opensearch_client", None):
        response = client.get(f"{_BASE}/filters", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body == {"speakers": [], "tags": [], "date_range": {}}


# ---------------------------------------------------------------------------
# Admin gating on the lifecycle verbs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", f"{_BASE}/models/neural/{_KNOWN_MODEL}/register"),
        ("post", f"{_BASE}/models/neural/{_KNOWN_MODEL}/deploy"),
        ("post", f"{_BASE}/models/neural/{_KNOWN_MODEL}/undeploy"),
        ("put", f"{_BASE}/models/neural/active"),
        ("post", f"{_BASE}/repair-indices"),
    ],
)
def test_admin_only_routes_reject_a_plain_user(client, user_token_headers, method, path):
    response = getattr(client, method)(
        path, headers=user_token_headers, params={"model_name": _KNOWN_MODEL}
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_neural_status_and_list_allow_a_plain_user(client, user_token_headers):
    """``GET /models/neural`` and ``GET /models/neural/status`` are read
    surfaces gated only by ``get_current_active_user`` — confirmed in
    ``search.py``, unlike the mutating verbs beside them."""
    assert client.get(f"{_BASE}/models/neural", headers=user_token_headers).status_code == 200
    assert (
        client.get(f"{_BASE}/models/neural/status", headers=user_token_headers).status_code == 200
    )


# ---------------------------------------------------------------------------
# 409 contract: registered-but-not-deployed / not-registered-at-all
# ---------------------------------------------------------------------------


def test_setting_active_model_undeployed_is_409(client, admin_token_headers):
    """``PUT .../active`` shares ``apply_embedding_model_switch`` with the legacy
    ``POST /models`` endpoint (#437): a model that is registered but not
    deployed is a 409 raised BEFORE anything is persisted, never a silent
    downgrade to the previous model."""
    with (
        patch(
            "app.services.search.ml_model_service.OpenSearchMLModelService.find_model_by_name",
            return_value="some-model-id",
        ),
        patch(
            "app.services.search.ml_model_service.OpenSearchMLModelService.get_model_status",
            return_value={"deployed": False},
        ),
    ):
        response = client.put(
            f"{_BASE}/models/neural/active",
            headers=admin_token_headers,
            params={"model_name": _KNOWN_MODEL},
        )

    assert response.status_code == status.HTTP_409_CONFLICT


def test_setting_active_model_unknown_is_400(client, admin_token_headers):
    response = client.put(
        f"{_BASE}/models/neural/active",
        headers=admin_token_headers,
        params={"model_name": "not-a-real-model"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_deploying_an_unregistered_model_is_404(client, admin_token_headers):
    with _standin_ml_service(registered=False):
        response = client.post(
            f"{_BASE}/models/neural/{_KNOWN_MODEL}/deploy", headers=admin_token_headers
        )
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Path-wildcard safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["register", "deploy", "undeploy"])
def test_a_path_traversal_shaped_model_name_is_never_spliced_into_opensearch(
    client, admin_token_headers, verb
):
    """``{model_name:path}`` accepts slashes, so a traversal-shaped value like
    ``../../etc/passwd`` is syntactically a valid path segment. Confirm it
    surfaces as an ordinary refusal (400 outside the static registry for
    register, 404 "not registered" for deploy/undeploy — ``find_model_by_name``
    does an in-memory exact-match scan, never a raw string into a URL) and, for
    deploy/undeploy, that the stand-in service was asked to *find* that literal
    string rather than anything being reached with it spliced into a path.

    ``../`` must be **percent-encoded** (``%2e%2e%2f``) here: a literal
    ``../../etc/passwd`` never reaches the server at all — httpx/RFC 3986 dot-
    segment normalization collapses it client-side, turning the request into
    ``POST /api/search/etc/passwd/undeploy``, a genuinely different (404,
    unmatched-route) path than the one this test means to exercise. That
    normalization is itself a real, useful defense layer, just not the one
    this test is pinning — the encoded form proves the *server's* handling
    once a decoded traversal-shaped string does reach ``model_name``.
    """
    traversal = "%2e%2e%2fetc%2fpasswd"
    decoded = "../etc/passwd"

    if verb == "register":
        response = client.post(
            f"{_BASE}/models/neural/{traversal}/register", headers=admin_token_headers
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        return

    with _standin_ml_service(registered=False) as service:
        response = client.post(
            f"{_BASE}/models/neural/{traversal}/{verb}", headers=admin_token_headers
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert ("find_model_by_name", decoded) in service.calls


# ---------------------------------------------------------------------------
# repair-indices: admin-only, does not require a reachable OpenSearch to answer
# ---------------------------------------------------------------------------


def test_repair_indices_requires_authentication(client):
    assert client.post(f"{_BASE}/repair-indices").status_code == status.HTTP_401_UNAUTHORIZED


def test_repair_indices_without_a_client_is_503(client, admin_token_headers):
    with patch("app.services.opensearch_service.opensearch_client", None):
        response = client.post(f"{_BASE}/repair-indices", headers=admin_token_headers)
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
