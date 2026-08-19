"""An embedding model must be adoptable through the app, not only in theory (#453).

Three defects stacked up to make a non-default embedding model **impossible to select**,
and each one hid the next. All three were found by trying to switch a live deployment to
the multilingual model, which is what #453 needs.

1. **The admin routes 404ed for every model that exists.**
   ``POST /search/models/neural/{model_name}/{register,deploy,undeploy}`` used a plain
   ``{model_name}`` path parameter, which never matches ``/``. Every key in
   ``OPENSEARCH_EMBEDDING_MODELS`` is ``huggingface/sentence-transformers/<x>``, so all
   three routes were unreachable — for *every* model, not an edge case. URL-encoding does
   not help: Starlette decodes before routing. ``api/CLAUDE.md`` documented them as
   working and idempotent.

2. **The name → id lookup returned CHUNK ids.** ML Commons stores model *chunk*
   documents in the same index, carrying the same ``name`` as their parent, with ids
   ``<model_id>_<n>``. ``list_models`` used ``match_all``, so ``find_model_by_name``
   handed a chunk id to deploy → HTTP 500 → the model-switch guard answered **409
   forever**. Measured on a live cluster: **172 documents, nearly all chunks**, so with
   ``size: 100`` the real model documents could miss the window entirely.

3. The settings UI still has no register/deploy control — tracked separately; a 409
   telling an admin to POST two endpoints by hand is not a UI.

⚠️ **The 409 guard itself is correct and must not be "fixed" by removing it** (#437):
recording a selection whose pipeline cannot emit the new dimension makes the reindex
coordinator delete the chunks index and then fail every write. The bug was never the
guard — it was that nothing could satisfy it.

These are route-shape and query-shape assertions. They do not need a cluster, which is
the point: the defects were both structural, and both survived because every existing
test either substituted the search client or never named these routes at all.
"""

from __future__ import annotations

import pytest

from app.core.constants import OPENSEARCH_EMBEDDING_MODELS

_ADMIN_VERBS = ("register", "deploy", "undeploy")


def _model_admin_routes() -> list:
    """The three model-administration routes, off the real app."""
    from app.main import app

    return [
        route
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/search/models/neural/")
        and any(f"/{verb}" in getattr(route, "path", "") for verb in _ADMIN_VERBS)
    ]


def test_every_registry_key_contains_a_slash() -> None:
    """The premise. If this ever stops being true the defect below changes shape."""
    assert OPENSEARCH_EMBEDDING_MODELS, "empty registry would make every test here vacuous"
    for model_id in OPENSEARCH_EMBEDDING_MODELS:
        assert "/" in model_id, (
            f"{model_id!r} has no slash — the whole reason these routes needed :path"
        )


@pytest.mark.parametrize("verb", _ADMIN_VERBS)
def test_the_model_admin_routes_accept_a_slashed_name(verb: str) -> None:
    """The defect: a plain ``{model_name}`` cannot match any real model id.

    Asserted on the route's declared path rather than by issuing a request, because a
    404 from a missing route and a 404 from a rejected name are indistinguishable in a
    response — and it was exactly that ambiguity that let this survive.
    """
    matching = [r for r in _model_admin_routes() if r.path.endswith(f"/{verb}")]

    assert matching, f"no /{verb} route found at all — the path shape changed"
    for route in matching:
        assert "{model_name:path}" in route.path, (
            f"{route.path} uses a non-path parameter, so it 404s for every model in the "
            "registry (all of which contain '/')"
        )


@pytest.mark.parametrize("verb", _ADMIN_VERBS)
def test_a_real_model_id_actually_matches_the_route(verb: str) -> None:
    """The end-to-end shape check: a registry key must route.

    Guards the guard above — ``:path`` in the string is necessary but the compiled
    matcher is what decides, and this asserts the matcher.
    """
    model_id = next(iter(OPENSEARCH_EMBEDDING_MODELS))
    target = f"/api/search/models/neural/{model_id}/{verb}"

    matched = [r for r in _model_admin_routes() if r.path_regex.match(target)]

    assert len(matched) == 1, (
        f"{target} matched {len(matched)} routes ({[r.path for r in matched]}); expected "
        f"exactly one — zero means an admin cannot reach {verb} at all, and more than one "
        "means the request's destination depends on registration order"
    )
    # The captured parameter must be the WHOLE model id. A route that matched but bound
    # only the last segment would reach the handler and be rejected as an unknown model,
    # which looks like a validation problem rather than a routing one.
    assert matched[0].path_regex.match(target).group("model_name") == model_id


# ---------------------------------------------------------------------------
# The chunk-id lookup
# ---------------------------------------------------------------------------
class _FakeTransport:
    """Records the search body and replays an ML Commons index containing chunks."""

    def __init__(self, hits: list[dict]) -> None:
        self.hits = hits
        self.bodies: list[dict] = []

    def perform_request(self, _method: str, _path: str, body: dict) -> dict:
        self.bodies.append(body)
        query = body.get("query", {})
        must_not = query.get("bool", {}).get("must_not", [])
        excludes_chunks = any(
            clause.get("exists", {}).get("field") == "chunk_number" for clause in must_not
        )
        hits = [
            hit for hit in self.hits if not (excludes_chunks and "chunk_number" in hit["_source"])
        ]
        return {"hits": {"hits": hits[: body.get("size", 10)]}}


def _service_with(hits: list[dict]):
    """An ml_model_service wired to a fake transport, with no cluster involved."""
    from app.services.search.ml_model_service import OpenSearchMLModelService

    service = OpenSearchMLModelService.__new__(OpenSearchMLModelService)
    client = type("C", (), {})()
    client.transport = _FakeTransport(hits)
    service._client = client
    service._ensure_client = lambda: True  # type: ignore[method-assign]
    return service, client.transport


def _chunk(model_id: str, n: int, name: str) -> dict:
    """A model CHUNK document: same name as its parent, id suffixed, no model_state."""
    return {
        "_id": f"{model_id}_{n}",
        "_source": {"name": name, "chunk_number": n, "total_chunks": 9, "model_id": model_id},
    }


def _model(model_id: str, name: str, state: str = "DEPLOYED") -> dict:
    return {"_id": model_id, "_source": {"name": name, "model_state": state}}


def test_the_name_lookup_never_returns_a_chunk_id() -> None:
    """The defect, exactly as it presented: deploy got ``<model_id>_3`` and 500ed.

    The chunks are placed FIRST because that is the observed ordering — a ``match_all``
    on a real cluster returned chunks before any model document.
    """
    name = "huggingface/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    hits = [_chunk("REALID", n, name) for n in range(9)] + [_model("REALID", name)]
    service, _ = _service_with(hits)

    found = service.find_model_by_name(name)

    assert found == "REALID", (
        f"expected the model id, got {found!r} — a chunk id here is handed straight to "
        "deploy, which 500s, which leaves the model switch answering 409 forever"
    )


def test_the_query_excludes_chunks_rather_than_filtering_afterwards() -> None:
    """Filtering client-side would still lose models past the ``size`` window.

    Measured on a live cluster: 172 documents, nearly all chunks. With ``size: 100`` and
    post-hoc filtering, the real model documents need never appear in the response at
    all — the search has to exclude them server-side.
    """
    name = "huggingface/sentence-transformers/all-MiniLM-L6-v2"
    hits = [_chunk("REALID", n, name) for n in range(150)] + [_model("REALID", name)]
    service, transport = _service_with(hits)

    found = service.find_model_by_name(name)

    assert found == "REALID", (
        "the model document sat past the size window behind 150 chunks, which is the "
        "live cluster's actual shape"
    )
    must_not = transport.bodies[0].get("query", {}).get("bool", {}).get("must_not", [])
    assert any(c.get("exists", {}).get("field") == "chunk_number" for c in must_not), (
        f"chunks are not excluded in the query itself: {transport.bodies[0]}"
    )


def test_a_clean_index_still_lists_its_models() -> None:
    """The control: excluding chunks must not exclude models."""
    name = "huggingface/sentence-transformers/all-MiniLM-L6-v2"
    service, _ = _service_with([_model("REALID", name)])

    assert [m["model_id"] for m in service.list_models()] == ["REALID"]
    assert service.find_model_by_name(name) == "REALID"


def test_an_absent_model_is_still_reported_missing() -> None:
    """Never invent an id: a wrong one is worse than None, which callers handle."""
    service, _ = _service_with([_model("REALID", "some/other/model")])

    assert service.find_model_by_name("huggingface/sentence-transformers/not-there") is None


# ---------------------------------------------------------------------------
# The description charset — found by the FIRST real user pressing the button
# ---------------------------------------------------------------------------
def test_registry_descriptions_cannot_sink_a_registration() -> None:
    """ML Commons rejects the WHOLE registration over one character in the description.

    "Model description can only contain letters, numbers, spaces, and basic
    punctuation" — and two of seven registry descriptions violated it (`+` in
    "50+ languages", an em-dash in the L12 blurb), so the Settings UI's Download &
    deploy 500ed on its first real use. The description is pure metadata nobody's
    code reads back; it must never be able to fail the operation.
    """
    from app.services.search.ml_model_service import _DESCRIPTION_ALLOWED
    from app.services.search.ml_model_service import _safe_description

    assert OPENSEARCH_EMBEDDING_MODELS, "empty registry — the loop below would pass vacuously"
    for model_id, info in OPENSEARCH_EMBEDDING_MODELS.items():
        sanitized = _safe_description(str(info.get("description", "")))
        offending = sorted(set(_DESCRIPTION_ALLOWED.findall(sanitized)))
        assert not offending, (
            f"{model_id}: sanitized description still carries {offending}, which fails "
            "the whole ML Commons registration with action_request_validation_exception"
        )


def test_sanitizing_degrades_words_rather_than_fusing_them() -> None:
    """'50+ languages' must become '50 languages', never '50languages'."""
    from app.services.search.ml_model_service import _safe_description

    assert _safe_description("Fast. 50+ languages — good quality.") == (
        "Fast. 50 languages good quality."
    )
    # The control: an already-legal description passes through byte-identical.
    assert _safe_description("Fast, lightweight English model.") == (
        "Fast, lightweight English model."
    )
