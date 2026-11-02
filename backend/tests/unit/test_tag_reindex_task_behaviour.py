"""Characterization of the ``update_file_tags_index`` Celery task body.

Written before the tags backend rewrite, against a fake OpenSearch client and the test
session standing in for the task's own short-lived one. ``pins_current_behaviour`` names pin
D6: the task declares retries it never uses and reports success when the lookup failed.
"""

from __future__ import annotations

import contextlib

import pytest
from fixtures.tag_world import attach
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word

from app.core.config import settings
from app.tasks.search_file_field_tasks import update_file_tags_index


class _Indices:
    def __init__(self, fail: bool = False) -> None:
        self.refreshes: list[str] = []
        self.fail = fail

    def refresh(self, index):
        self.refreshes.append(index)
        if self.fail:
            raise RuntimeError("refresh refused")


class _Client:
    def __init__(self, updated=1, fail_for: set[int] | None = None, refresh_fails=False) -> None:
        self.updated = updated
        self.fail_for = fail_for or set()
        self.calls: list[dict] = []
        self.indices = _Indices(refresh_fails)

    def update_by_query(self, *, index, body, conflicts):
        file_id = body["query"]["term"]["file_id"]
        self.calls.append({"index": index, "body": body, "conflicts": conflicts})
        if file_id in self.fail_for:
            raise RuntimeError("opensearch refused")
        return {"updated": self.updated}


@pytest.fixture
def same_session(monkeypatch, db_session):
    @contextlib.contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", scope)


def _use_client(monkeypatch, client) -> None:
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: client)


def test_an_empty_file_list_is_skipped_before_anything_else(monkeypatch):
    def explode():
        raise AssertionError("the client must not be looked up")

    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", explode)

    assert update_file_tags_index([]) == {"status": "skipped", "reason": "no_file_ids"}


def test_no_opensearch_client_is_skipped(monkeypatch):
    _use_client(monkeypatch, None)

    assert update_file_tags_index([1]) == {"status": "skipped", "reason": "no_opensearch"}


def test_each_file_gets_one_update_by_query_with_its_tag_names(
    db_session, normal_user, monkeypatch, same_session
):
    client = _Client(updated=3)
    _use_client(monkeypatch, client)
    first, second = make_file(db_session, normal_user), make_file(db_session, normal_user)
    alpha, beta = word("alpha"), word("beta")
    attach(db_session, first, make_tag(db_session, alpha, user_id=normal_user.id))
    attach(db_session, first, make_tag(db_session, beta, user_id=normal_user.id))
    attach(db_session, second, make_tag(db_session, alpha + "-2", user_id=normal_user.id))

    result = update_file_tags_index([first.id, second.id])

    assert result == {"status": "success", "updated": 6, "files": 2, "errors": 0}
    assert [c["body"]["query"] for c in client.calls] == [
        {"term": {"file_id": first.id}},
        {"term": {"file_id": second.id}},
    ]
    assert {c["index"] for c in client.calls} == {settings.OPENSEARCH_CHUNKS_INDEX}
    assert {c["conflicts"] for c in client.calls} == {"proceed"}
    for call in client.calls:
        assert call["body"]["script"]["source"] == "ctx._source.tags = params.tags"
        assert call["body"]["script"]["lang"] == "painless"
    assert sorted(client.calls[0]["body"]["script"]["params"]["tags"]) == sorted([alpha, beta])
    assert client.calls[1]["body"]["script"]["params"]["tags"] == [alpha + "-2"]


def test_a_file_with_no_tags_is_written_an_empty_list(
    db_session, normal_user, monkeypatch, same_session
):
    client = _Client()
    _use_client(monkeypatch, client)
    bare = make_file(db_session, normal_user)

    result = update_file_tags_index([bare.id])

    assert result["status"] == "success"
    assert client.calls[0]["body"]["script"]["params"] == {"tags": []}


def test_the_index_is_refreshed_once_and_only_when_something_was_updated(
    db_session, normal_user, monkeypatch, same_session
):
    files = [make_file(db_session, normal_user) for _ in range(3)]
    updated_client = _Client(updated=1)
    _use_client(monkeypatch, updated_client)

    update_file_tags_index([f.id for f in files])
    assert updated_client.indices.refreshes == [settings.OPENSEARCH_CHUNKS_INDEX]

    idle_client = _Client(updated=0)
    _use_client(monkeypatch, idle_client)
    update_file_tags_index([f.id for f in files])
    assert idle_client.indices.refreshes == []


def test_a_refresh_failure_is_only_a_warning(db_session, normal_user, monkeypatch, same_session):
    _use_client(monkeypatch, _Client(updated=1, refresh_fails=True))
    media_file = make_file(db_session, normal_user)

    result = update_file_tags_index([media_file.id])

    assert result == {"status": "success", "updated": 1, "files": 1, "errors": 0}


def test_a_failing_file_is_counted_and_the_next_one_still_runs(
    db_session, normal_user, monkeypatch, same_session
):
    first, second, third = (make_file(db_session, normal_user) for _ in range(3))
    client = _Client(updated=2, fail_for={second.id})
    _use_client(monkeypatch, client)

    result = update_file_tags_index([first.id, second.id, third.id])

    assert result == {"status": "success", "updated": 4, "files": 3, "errors": 1}
    assert [c["body"]["query"]["term"]["file_id"] for c in client.calls] == [
        first.id,
        second.id,
        third.id,
    ]


def test_a_failed_tag_lookup_counts_every_file_and_updates_nothing_pins_current_behaviour(
    monkeypatch,
):
    """D6: the status is still ``success`` and no retry is attempted."""

    @contextlib.contextmanager
    def broken_scope():
        raise RuntimeError("database is down")
        yield  # pragma: no cover

    monkeypatch.setattr("app.db.session_utils.session_scope", broken_scope)
    client = _Client()
    _use_client(monkeypatch, client)

    result = update_file_tags_index([11, 12, 13])

    assert result == {"status": "success", "updated": 0, "files": 3, "errors": 3}
    assert client.calls == []
    assert client.indices.refreshes == []


def test_the_task_is_registered_with_its_declared_retry_settings():
    assert update_file_tags_index.name == "update_file_tags_index"
    assert update_file_tags_index.max_retries == 3
    assert update_file_tags_index.default_retry_delay == 10
