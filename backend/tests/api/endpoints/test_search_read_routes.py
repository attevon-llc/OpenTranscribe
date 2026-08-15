"""Functional tests for ``GET /api/search/count`` and ``GET /api/search/suggestions``.

Both were listed by ``scripts/audit-route-coverage.py`` as referenced by no test.
They are the two "as you type" reads: the in-page find bar polls ``/count`` on every
keystroke to learn whether matches exist past the segments the browser has loaded,
and the search box polls ``/suggestions`` for autocomplete.

Split out of ``test_search_admin_routes.py`` deliberately: these are
``get_current_active_user`` reads with no side effect, while everything in that file
is an admin verb that starts or stops deployment-wide work. Mixing them would put a
reindex dispatch and a keystroke poll under one module docstring.

The scoping tests substitute the search engine, because "which filters went to
OpenSearch" is not observable from a response body — and the filter that scopes a
count to the caller is the one that must never be dropped. The tests that do NOT
substitute it assert only what holds against any cluster, including none:
``count_matches`` and ``get_suggestions`` both degrade to ``0`` / ``[]`` without a
client rather than raising, so this module needs no ``SKIP_OPENSEARCH`` gate.
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import patch

from fastapi import status

COUNT = "/api/search/count"
SUGGESTIONS = "/api/search/suggestions"


class _StandInEngine:
    """Records the search bodies it is given and answers with a canned total."""

    def __init__(self, total: int = 0, hits: list | None = None) -> None:
        self.total = total
        self.hits = hits or []
        self.bodies: list[dict] = []

    def search(self, *, index: str, body: dict) -> dict:  # noqa: ARG002 - index unused
        self.bodies.append(body)
        return {"hits": {"total": {"value": self.total}, "hits": self.hits}}


def _standin_count_engine(engine: _StandInEngine):
    """Substitute the engine for ``count_matches`` only, index checks included."""
    return (
        patch(
            "app.services.search.hybrid_search_service.get_opensearch_client",
            return_value=engine,
        ),
        patch("app.services.search.hybrid_search_service._ensure_infrastructure", lambda: None),
    )


# ---------------------------------------------------------------------------
# GET /count
# ---------------------------------------------------------------------------
def test_count_of_an_unmatchable_query_is_zero(client, user_token_headers):
    """The shape the find bar reads, against whatever cluster is configured.

    A random token cannot appear in any transcript, so ``0`` is the answer with or
    without OpenSearch — but the response must still be the ``{"total": int}``
    envelope. The find bar does ``body.total > loaded`` with no guard, so a bare
    integer or a renamed key breaks it silently.
    """
    response = client.get(COUNT, headers=user_token_headers, params={"q": uuid_pkg.uuid4().hex})

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"total": 0}


def test_count_returns_the_engines_total(client, user_token_headers):
    """The handler reports the engine's count rather than the page size.

    Catches the handler counting returned hits instead of reading
    ``track_total_hits`` — the find bar would then cap at the page size and stop
    reporting "more matches below" on long transcripts. Engine substituted.
    """
    engine = _StandInEngine(total=137)
    index_patch, infra_patch = _standin_count_engine(engine)

    with index_patch, infra_patch:
        response = client.get(COUNT, headers=user_token_headers, params={"q": "pricing"})

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"total": 137}
    assert engine.bodies[0]["size"] == 0
    assert engine.bodies[0]["track_total_hits"] is True


def test_count_is_scoped_to_the_caller_and_optionally_one_file(
    client, user_token_headers, normal_user
):
    """Every count carries the caller's own id, and ``file_uuid`` narrows to one file.

    This is the isolation invariant: without the ``accessible_user_ids`` filter the
    find bar would report matches from other accounts' transcripts. Not observable
    from the response, hence the substituted engine.
    """
    engine = _StandInEngine(total=1)
    index_patch, infra_patch = _standin_count_engine(engine)
    file_uuid = "22222222-2222-4222-8222-222222222222"

    with index_patch, infra_patch:
        client.get(COUNT, headers=user_token_headers, params={"q": "pricing"})
        client.get(
            COUNT, headers=user_token_headers, params={"q": "pricing", "file_uuid": file_uuid}
        )

    unscoped, scoped = (b["query"]["bool"]["filter"] for b in engine.bodies)
    assert {"terms": {"accessible_user_ids": [normal_user.id]}} in unscoped
    assert {"term": {"file_uuid": file_uuid}} not in unscoped
    assert {"term": {"file_uuid": file_uuid}} in scoped


def test_count_rejects_a_missing_query(client, user_token_headers):
    response = client.get(COUNT, headers=user_token_headers)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_count_rejects_an_empty_query(client, user_token_headers):
    """``min_length=1``: an empty find bar must not issue a corpus-wide match_all."""
    response = client.get(COUNT, headers=user_token_headers, params={"q": ""})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_count_requires_authentication(client):
    response = client.get(COUNT, params={"q": "pricing"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /suggestions
# ---------------------------------------------------------------------------
def test_suggestions_for_an_unmatchable_prefix_is_an_empty_list(client, user_token_headers):
    """A JSON array is the contract; the SPA maps over it without a null check."""
    response = client.get(
        SUGGESTIONS, headers=user_token_headers, params={"q": uuid_pkg.uuid4().hex}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == []


def test_suggestions_rejects_a_one_character_prefix(client, user_token_headers):
    """``min_length=2`` — one character would match most of the corpus."""
    response = client.get(SUGGESTIONS, headers=user_token_headers, params={"q": "a"})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_suggestions_rejects_a_limit_above_the_ceiling(client, user_token_headers):
    """``le=20``: the dropdown is bounded server-side, not by the caller."""
    response = client.get(SUGGESTIONS, headers=user_token_headers, params={"q": "pri", "limit": 21})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_suggestions_rejects_a_zero_limit(client, user_token_headers):
    response = client.get(SUGGESTIONS, headers=user_token_headers, params={"q": "pri", "limit": 0})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_suggestions_drops_a_quarantined_files_title_for_a_plain_user(
    client, db_session, user_token_headers, normal_user
):
    """A taken-down file must not surface in autocomplete.

    The chunks index carries no quarantine field, so the handler re-checks against
    Postgres — the DMCA/abuse path's only defence on this surface. The engine is
    substituted so the two candidate titles are known; the quarantine decision under
    test is made in the handler against a real row.
    """
    from tests.user_owned_rows import make_media_file

    hidden = make_media_file(db_session, int(normal_user.id))
    visible = make_media_file(db_session, int(normal_user.id))
    hidden.is_quarantined = True
    db_session.commit()

    suggestions = [
        {"type": "title", "text": "hidden recording", "file_uuid": str(hidden.uuid)},
        {"type": "title", "text": "visible recording", "file_uuid": str(visible.uuid)},
        {"type": "speaker", "text": "Dana"},
    ]
    with patch(
        "app.services.search.hybrid_search_service.HybridSearchService.get_suggestions",
        return_value=suggestions,
    ):
        response = client.get(SUGGESTIONS, headers=user_token_headers, params={"q": "rec"})

    assert response.status_code == status.HTTP_200_OK
    texts = [s["text"] for s in response.json()]
    assert texts == ["visible recording", "Dana"]


def test_suggestions_keeps_a_quarantined_title_for_an_admin(
    client, db_session, admin_token_headers, admin_user
):
    """The control for the filter above: admins keep visibility for review.

    Without this, a handler that dropped every ``file_uuid``-bearing suggestion
    would pass the test above and quietly break the moderation view.
    """
    from tests.user_owned_rows import make_media_file

    hidden = make_media_file(db_session, int(admin_user.id))
    hidden.is_quarantined = True
    db_session.commit()

    suggestions = [{"type": "title", "text": "hidden recording", "file_uuid": str(hidden.uuid)}]
    with patch(
        "app.services.search.hybrid_search_service.HybridSearchService.get_suggestions",
        return_value=suggestions,
    ):
        response = client.get(SUGGESTIONS, headers=admin_token_headers, params={"q": "rec"})

    assert response.status_code == status.HTTP_200_OK
    assert [s["text"] for s in response.json()] == ["hidden recording"]


def test_suggestions_requires_authentication(client):
    response = client.get(SUGGESTIONS, params={"q": "pri"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_suggestions_engine_is_asked_for_the_callers_own_scope(
    client, user_token_headers, normal_user
):
    """The prefix, limit and caller identity are all passed through, unmodified.

    A dropped ``user_id`` would offer one account's titles to another; a dropped
    ``limit`` would let the dropdown grow unbounded. Recorded through a real
    stand-in rather than a mock assertion.
    """
    calls: list[dict] = []

    def _record(_self, *, prefix, user_id, limit, organization_id):
        calls.append(
            {
                "prefix": prefix,
                "user_id": user_id,
                "limit": limit,
                "organization_id": organization_id,
            }
        )
        return []

    with patch(
        "app.services.search.hybrid_search_service.HybridSearchService.get_suggestions", _record
    ):
        response = client.get(
            SUGGESTIONS, headers=user_token_headers, params={"q": "pric", "limit": 5}
        )

    assert response.status_code == status.HTTP_200_OK
    assert calls == [
        {"prefix": "pric", "user_id": normal_user.id, "limit": 5, "organization_id": None}
    ]
