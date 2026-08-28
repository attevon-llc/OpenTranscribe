"""Operator-triggered re-embed of neural-search degraded (text-only) files (issue #626).

Mirrors ``test_speaker_id_fields.py``'s backfill-task section: the survey function, the
thin dispatch task, and the two new endpoints. The single most important test here is
predicate correctness (#1) — the issue framing was originally heading toward matching the
literal string ``"neural"``, which is :data:`EMBEDDING_MODEL_UNKNOWN` (a document that DID
get embedded, just unattributed), not the actually-degraded "no vector at all" case. #2
proves it fails red against that wrong predicate before trusting it green against the real
one.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

from opensearchpy.exceptions import ConnectionError as OSConnectionError

from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
from app.services.search import embedding_provenance as prov
from app.utils.task_utils import TASK_STATUS_SKIPPED

# --------------------------------------------------------------------------- #
# 1 & 2. Predicate correctness — the finding this whole issue is about.
# --------------------------------------------------------------------------- #


def _mock_client(*, exists: bool = True) -> MagicMock:
    client = MagicMock()
    client.indices.exists.return_value = exists
    return client


def _composite_response(buckets: list[dict[str, Any]], after_key: dict[str, Any] | None) -> dict:
    files_agg: dict[str, Any] = {"buckets": buckets}
    if after_key is not None:
        files_agg["after_key"] = after_key
    return {"aggregations": {"files": files_agg}}


def test_degraded_predicate_is_missing_field_not_the_literal_neural_string():
    """The predicate itself: ``must_not exists(embedding_model)``, never a term match."""
    assert prov.DEGRADED_PREDICATE_MUST_NOT == [{"exists": {"field": "embedding_model"}}]
    body_str = str(prov.DEGRADED_PREDICATE_MUST_NOT)
    assert "neural" not in body_str


def test_survey_degraded_files_sends_the_correct_query_body():
    """Reproduces the exact three-way index split from the issue: absent / "neural"
    (unattributed-but-embedded) / a real model name — and asserts the query this function
    sends would select ONLY the absent one, by inspecting the body it builds.

    A wrong predicate (``{"term": {"embedding_model": "neural"}}``) would select the
    SECOND document and skip the first — this test would go red under that predicate
    because it asserts the exact `must_not` shape, not just "some result came back".
    """
    client = _mock_client()
    client.search.return_value = _composite_response(
        [{"key": {"user_id": 1, "file_uuid": "absent-doc"}, "doc_count": 1}], after_key=None
    )
    with patch("app.services.opensearch_service.opensearch_client", client):
        files, truncated = prov.survey_degraded_files(limit=50)

    assert files == [prov.DegradedFile(user_id=1, file_uuid="absent-doc")]
    assert truncated is False

    body = client.search.call_args.kwargs["body"]
    query_bool = body["query"]["bool"]
    assert query_bool["must_not"] == [{"exists": {"field": "embedding_model"}}]
    # And NOT a term match on the literal "neural" sentinel anywhere in the query.
    assert "neural" not in str(query_bool)
    # Uses the compat-armed chunk-plane predicate, not a bare doc_type term.
    assert chunk_plane_clause() in query_bool["filter"]


def test_survey_degraded_files_would_be_wrong_against_a_naive_term_predicate():
    """Explicit red/green: simulate what a ``term: neural`` predicate WOULD select, and
    show it disagrees with the real one on the issue's own three-document fixture.
    """
    absent_doc: dict[str, Any] = {"_id": "d1", "embedding_model_present": False}
    unattributed_doc: dict[str, Any] = {"_id": "d2", "embedding_model": "neural"}
    named_doc: dict[str, Any] = {"_id": "d3", "embedding_model": "all-MiniLM-L6-v2"}
    docs: list[dict[str, Any]] = [absent_doc, unattributed_doc, named_doc]

    def matches_real_predicate(doc: dict[str, Any]) -> bool:
        return not doc.get("embedding_model_present", "embedding_model" in doc)

    def matches_wrong_predicate(doc: dict[str, Any]) -> bool:
        return doc.get("embedding_model") == "neural"

    real_matches = {d["_id"] for d in docs if matches_real_predicate(d)}
    wrong_matches = {d["_id"] for d in docs if matches_wrong_predicate(d)}

    assert real_matches == {"d1"}
    assert wrong_matches == {"d2"}
    assert real_matches != wrong_matches


# --------------------------------------------------------------------------- #
# Fail-closed behaviour.
# --------------------------------------------------------------------------- #


def test_survey_degraded_files_fails_closed_with_no_client():
    with patch("app.services.opensearch_service.opensearch_client", None):
        files, truncated = prov.survey_degraded_files()
    assert files == []
    assert truncated is False


def test_survey_degraded_files_fails_closed_when_index_absent():
    client = _mock_client(exists=False)
    with patch("app.services.opensearch_service.opensearch_client", client):
        files, truncated = prov.survey_degraded_files()
    assert files == []
    assert truncated is False
    client.search.assert_not_called()


def test_survey_degraded_files_fails_closed_on_opensearch_error():
    """An OpenSearch outage must return ([], False), and a caller must never confuse
    that with "genuinely nothing is degraded" — the whole point of a `(files, truncated)`
    tuple rather than a bare list is that a truncated/failed survey is distinguishable.
    """
    client = _mock_client()
    client.search.side_effect = OSConnectionError("N/A", "cluster unreachable", None)
    with patch("app.services.opensearch_service.opensearch_client", client):
        files, truncated = prov.survey_degraded_files()
    assert files == []
    assert truncated is False


# --------------------------------------------------------------------------- #
# Pre-v6 compat: no doc_type at all, and no embedding_model, is still found.
# --------------------------------------------------------------------------- #


def test_survey_degraded_files_finds_pre_v6_documents_with_no_doc_type():
    """`chunk_plane_clause()`'s compat arm must still be in effect — a legacy document
    predating the `doc_type` field entirely must not be excluded from the survey.
    """
    client = _mock_client()
    client.search.return_value = _composite_response(
        [{"key": {"user_id": 5, "file_uuid": "legacy-no-doctype"}, "doc_count": 1}],
        after_key=None,
    )
    with patch("app.services.opensearch_service.opensearch_client", client):
        files, _truncated = prov.survey_degraded_files(limit=50)

    assert files == [prov.DegradedFile(user_id=5, file_uuid="legacy-no-doctype")]
    body = client.search.call_args.kwargs["body"]
    filter_clauses = body["query"]["bool"]["filter"]
    assert chunk_plane_clause() in filter_clauses
    # The compat clause matches both an explicit doc_type=chunk AND a missing doc_type.
    compat = chunk_plane_clause()
    should = compat["bool"]["should"]
    assert {"bool": {"must_not": {"exists": {"field": "doc_type"}}}} in should


# --------------------------------------------------------------------------- #
# truncated=True when population exceeds limit.
# --------------------------------------------------------------------------- #


def test_survey_degraded_files_reports_truncated_when_limit_is_hit_with_more_pages():
    client = _mock_client()
    page_one = _composite_response(
        [{"key": {"user_id": 1, "file_uuid": "aaaa"}, "doc_count": 1}],
        after_key={"user_id": 1, "file_uuid": "aaaa"},
    )
    with patch("app.services.opensearch_service.opensearch_client", client):
        client.search.return_value = page_one
        files, truncated = prov.survey_degraded_files(limit=1)

    assert len(files) == 1
    assert truncated is True


def test_survey_degraded_files_not_truncated_when_population_ends_exactly_at_limit():
    client = _mock_client()
    page = _composite_response(
        [{"key": {"user_id": 1, "file_uuid": "aaaa"}, "doc_count": 1}],
        after_key=None,
    )
    with patch("app.services.opensearch_service.opensearch_client", client):
        client.search.return_value = page
        files, truncated = prov.survey_degraded_files(limit=1)

    assert len(files) == 1
    assert truncated is False


def test_survey_degraded_files_paginates_via_after_key():
    client = _mock_client()
    page_one = _composite_response(
        [{"key": {"user_id": 1, "file_uuid": "aaaa"}, "doc_count": 1}],
        after_key={"user_id": 1, "file_uuid": "aaaa"},
    )
    page_two = _composite_response(
        [{"key": {"user_id": 1, "file_uuid": "bbbb"}, "doc_count": 1}],
        after_key=None,
    )
    client.search.side_effect = [page_one, page_two]
    with patch("app.services.opensearch_service.opensearch_client", client):
        files, truncated = prov.survey_degraded_files(limit=50)

    assert client.search.call_count == 2
    second_call_composite = client.search.call_args_list[1].kwargs["body"]["aggs"]["files"][
        "composite"
    ]
    assert second_call_composite["after"] == {"user_id": 1, "file_uuid": "aaaa"}
    assert {f.file_uuid for f in files} == {"aaaa", "bbbb"}
    assert truncated is False


# --------------------------------------------------------------------------- #
# The task: grouping/dispatch, two users, lock, isolation, empty-population.
# --------------------------------------------------------------------------- #


def _patch_session_scope(target_module):
    """The task opens three short session_scope()s; patch it to a no-op contextmanager
    plus a stand-in Task object so create_task_record/update_task_status don't need a
    real DB for these unit tests.
    """
    import contextlib

    fake_db = MagicMock()

    @contextlib.contextmanager
    def _scope():
        yield fake_db

    return patch(f"{target_module}.session_scope", _scope), fake_db


def test_task_dispatches_one_reindex_call_per_owner_with_two_users():
    """TWO users, not one — a single-user fixture could pass a broken implementation
    that never actually grouped by owner.
    """
    from app.tasks import search_reembed_task as task_mod

    files = [
        prov.DegradedFile(user_id=1, file_uuid="aaaa"),
        prov.DegradedFile(user_id=1, file_uuid="bbbb"),
        prov.DegradedFile(user_id=2, file_uuid="cccc"),
    ]

    scope_patch, fake_db = _patch_session_scope("app.tasks.search_reembed_task")
    fake_task = MagicMock(id="task-123")

    with (
        scope_patch,
        patch.object(task_mod, "create_task_record", return_value=fake_task) as mock_create,
        patch.object(task_mod, "update_task_status") as mock_update,
        patch(
            "app.services.search.embedding_provenance.survey_degraded_files",
            return_value=(files, False),
        ),
        patch("app.tasks.reindex_task.reindex_transcripts_task") as mocked_reindex,
    ):
        self_stub = MagicMock()
        self_stub.request.id = "celery-task-id"
        result = task_mod._reembed_degraded_files(self_stub, triggered_by=99)

    assert result["status"] == "dispatched"
    assert result["dispatched_files"] == 3
    assert result["dispatched_users"] == 2
    assert result["dispatch_failures"] == []
    assert mocked_reindex.apply_async.call_count == 2

    dispatched = {
        call.kwargs["args"][0]: set(call.kwargs["args"][1])
        for call in mocked_reindex.apply_async.call_args_list
    }
    assert dispatched[1] == {"aaaa", "bbbb"}
    assert dispatched[2] == {"cccc"}
    mock_create.assert_called_once()
    assert mock_update.call_count >= 2  # in_progress, then completed


def test_task_reports_skipped_with_a_real_completed_task_row_on_empty_population(db_session):
    """Uses the REAL DB session (db_session fixture), not a mock, so this asserts a real
    Task row with completed_at set — not just the returned dict.
    """
    from app.core.security import get_password_hash
    from app.models.media import Task
    from app.models.user import User
    from app.tasks import search_reembed_task as task_mod

    user = User(
        email="reembed-empty-fixture@example.com",
        full_name="Reembed Fixture",
        hashed_password=get_password_hash("password123"),  # noqa: S106
        is_active=True,
        role="admin",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    with (
        patch("app.tasks.search_reembed_task.session_scope") as mock_scope,
        patch(
            "app.services.search.embedding_provenance.survey_degraded_files",
            return_value=([], False),
        ),
    ):
        import contextlib

        @contextlib.contextmanager
        def _scope():
            yield db_session

        mock_scope.side_effect = _scope

        self_stub = MagicMock()
        self_stub.request.id = "celery-empty-task-id"
        result = task_mod._reembed_degraded_files(self_stub, triggered_by=user.id)

    assert result == {"status": "skipped", "reason": "no_degraded_files"}

    row = db_session.query(Task).filter(Task.id == "celery-empty-task-id").first()
    assert row is not None
    assert row.status == TASK_STATUS_SKIPPED
    assert row.completed_at is not None


def test_task_lock_already_held_prevents_dispatch(monkeypatch):
    """The Redis lock, not a second ad hoc flag — apply_async must not be called at all
    when the lock is already held. Mirrors
    ``test_search_index_health_repair.py::test_the_maintenance_task_declines_when_the_lock_is_held``.
    """
    import contextlib

    from app.tasks import search_reembed_task as task_mod
    from app.utils import task_lock

    @contextlib.contextmanager
    def _busy(lock_key: str, timeout: int = 300, blocking_timeout: int = 0):  # noqa: ARG001
        yield False

    monkeypatch.setattr(task_lock.task_lock_manager, "acquire_lock", _busy)

    with patch("app.tasks.reindex_task.reindex_transcripts_task") as mocked_reindex:
        result = task_mod.reembed_degraded_files_task(triggered_by=1)

    assert result["skipped"] is True
    mocked_reindex.apply_async.assert_not_called()


def test_task_isolates_a_per_owner_dispatch_failure():
    """User A's apply_async raises; user B must still be dispatched, and the failure
    must be recorded rather than silently swallowed or aborting the whole run.
    """
    from app.tasks import search_reembed_task as task_mod

    files = [
        prov.DegradedFile(user_id=1, file_uuid="aaaa"),
        prov.DegradedFile(user_id=2, file_uuid="bbbb"),
    ]

    scope_patch, _fake_db = _patch_session_scope("app.tasks.search_reembed_task")
    fake_task = MagicMock(id="task-456")

    with (
        scope_patch,
        patch.object(task_mod, "create_task_record", return_value=fake_task),
        patch.object(task_mod, "update_task_status"),
        patch(
            "app.services.search.embedding_provenance.survey_degraded_files",
            return_value=(files, False),
        ),
        patch("app.tasks.reindex_task.reindex_transcripts_task") as mocked_reindex,
    ):

        def _apply_async(*, args, priority):
            if args[0] == 1:
                raise RuntimeError("broker unavailable")

        mocked_reindex.apply_async.side_effect = _apply_async

        self_stub = MagicMock()
        self_stub.request.id = "celery-task-id"
        result = task_mod._reembed_degraded_files(self_stub, triggered_by=1)

    assert result["status"] == "dispatched"
    assert result["dispatched_users"] == 1
    assert result["dispatched_files"] == 1
    assert len(result["dispatch_failures"]) == 1
    assert result["dispatch_failures"][0]["user_id"] == 1
    assert mocked_reindex.apply_async.call_count == 2


# --------------------------------------------------------------------------- #
# Endpoint auth: non-admin gets 403 on both routes.
# --------------------------------------------------------------------------- #


def test_get_degraded_embeddings_requires_admin(client, user_token_headers):
    response = client.get("/api/search/degraded-embeddings", headers=user_token_headers)
    assert response.status_code == 403


def test_post_reembed_degraded_requires_admin(client, user_token_headers):
    response = client.post("/api/search/reembed-degraded", headers=user_token_headers)
    assert response.status_code == 403


def test_get_degraded_embeddings_unauthenticated_is_rejected(client):
    response = client.get("/api/search/degraded-embeddings")
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Endpoint: truncated flag surfaces, and already_running / no_degraded_files states.
# --------------------------------------------------------------------------- #


def test_get_degraded_embeddings_surfaces_truncated(client, admin_token_headers):
    import uuid as uuid_pkg

    files = [prov.DegradedFile(user_id=1, file_uuid=str(uuid_pkg.uuid4()))]
    with patch(
        "app.services.search.embedding_provenance.survey_degraded_files",
        return_value=(files, True),
    ):
        response = client.get(
            "/api/search/degraded-embeddings?limit=1", headers=admin_token_headers
        )
    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is True
    assert body["total_files"] == 1


def test_trigger_reembed_degraded_reports_no_degraded_files(client, admin_token_headers):
    with (
        patch("app.utils.task_lock.task_lock_manager.is_locked", return_value=False),
        patch(
            "app.services.search.embedding_provenance.survey_degraded_files",
            return_value=([], False),
        ),
    ):
        response = client.post("/api/search/reembed-degraded", headers=admin_token_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_degraded_files"
    assert body["task_id"] is None


def test_trigger_reembed_degraded_reports_already_running(client, admin_token_headers):
    with patch("app.utils.task_lock.task_lock_manager.is_locked", return_value=True):
        response = client.post("/api/search/reembed-degraded", headers=admin_token_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "already_running"
    assert body["task_id"] is None


def test_trigger_reembed_degraded_dispatches_when_files_found(client, admin_token_headers):
    files = [prov.DegradedFile(user_id=1, file_uuid="aaaa")]
    with (
        patch("app.utils.task_lock.task_lock_manager.is_locked", return_value=False),
        patch(
            "app.services.search.embedding_provenance.survey_degraded_files",
            return_value=(files, False),
        ),
        patch("app.tasks.search_reembed_task.reembed_degraded_files_task") as mocked_task,
    ):
        mocked_task.apply_async.return_value = MagicMock(id="dispatched-task-id")
        response = client.post("/api/search/reembed-degraded", headers=admin_token_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "started"
    assert body["task_id"] == "dispatched-task-id"
    mocked_task.apply_async.assert_called_once()
