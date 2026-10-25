"""The audit event carries typed resource fields, and the org read honours them (#1277).

``resource_type`` / ``resource_uuid`` / ``tenant_scope`` are top-level keyword fields so "all
that happened to this file" is one filter and a personal-scope event is never attributed to an
organization through its actor (finding F5). These tests pin the writer, the index mapping and
the query clause with no cluster; ``tests/integration/test_audit_org_scope_opensearch.py``
evaluates the same clause against a real index.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest

from app.auth import audit as audit_module
from app.auth.audit import AuditEventType
from app.auth.audit import AuditLogger
from app.auth.audit import AuditOutcome
from app.auth.audit import build_org_scope_clause
from app.models.media import MediaFile
from app.models.user import User


@pytest.fixture()
def logger_and_events(monkeypatch):
    monkeypatch.setattr(audit_module.settings, "AUDIT_LOG_ENABLED", True)
    monkeypatch.setattr(audit_module.settings, "AUDIT_LOG_TO_OPENSEARCH", True)
    logger = AuditLogger()
    events: list[dict] = []
    monkeypatch.setattr(logger, "_index_to_opensearch", events.append)
    return logger, events


def test_typed_fields_are_written_top_level_and_omitted_when_unset(logger_and_events):
    logger, events = logger_and_events

    logger.log(
        AuditEventType.CONTENT_FILE_VIEW,
        AuditOutcome.SUCCESS,
        user_id=1,
        organization_id=7,
        resource_type="media_file",
        resource_uuid="abc",
        tenant_scope="org",
    )
    logger.log(AuditEventType.AUTH_LOGOUT, AuditOutcome.SUCCESS, user_id=1)

    typed, plain = events
    assert (typed["resource_type"], typed["resource_uuid"], typed["tenant_scope"]) == (
        "media_file",
        "abc",
        "org",
    )
    assert not {"resource_type", "resource_uuid", "tenant_scope"} & plain.keys()


class _Indices:
    def __init__(self, exists: bool, put_error: Exception | None = None):
        self._exists = exists
        self._put_error = put_error
        self.created: list[dict] = []
        self.put: list[dict] = []

    def exists(self, index):
        return self._exists

    def create(self, index, body):
        self.created.append(body)

    def put_mapping(self, index, body):
        if self._put_error:
            raise self._put_error
        self.put.append(body)


def test_a_new_index_is_created_with_the_typed_keyword_fields():
    indices = _Indices(exists=False)

    AuditLogger()._ensure_index(SimpleNamespace(indices=indices), "audit-logs-2099.01")

    props = indices.created[0]["mappings"]["properties"]
    assert {props[f]["type"] for f in ("resource_type", "resource_uuid", "tenant_scope")} == {
        "keyword"
    }
    assert indices.put == []


def test_an_existing_index_gains_the_fields_once_per_process():
    indices = _Indices(exists=True)
    logger = AuditLogger()
    client = SimpleNamespace(indices=indices)

    logger._ensure_index(client, "audit-logs-2099.02")
    logger._ensure_index(client, "audit-logs-2099.02")

    assert len(indices.put) == 1
    assert set(indices.put[0]["properties"]) == {"resource_type", "resource_uuid", "tenant_scope"}
    assert indices.created == []


def test_a_mapping_conflict_is_logged_and_does_not_stop_indexing(caplog):
    indices = _Indices(exists=True, put_error=RuntimeError("mapper conflict"))
    logger = AuditLogger()

    with caplog.at_level(logging.WARNING):
        logger._ensure_index(SimpleNamespace(indices=indices), "audit-logs-2099.03")

    assert "Could not add typed content-audit fields" in caplog.text
    assert "audit-logs-2099.03" in logger._known_indices


def test_the_legacy_branch_refuses_an_event_that_names_its_own_tenant():
    """F5: a personal event has no organization_id, so only tenant_scope keeps it out."""
    clause = build_org_scope_clause(7, [1, 2])
    legacy = clause["bool"]["should"][1]["bool"]

    assert {"exists": {"field": "organization_id"}} in legacy["must_not"]
    assert {"exists": {"field": "tenant_scope"}} in legacy["must_not"]
    assert legacy["filter"] == [{"terms": {"user_id": [1, 2]}}]


def test_the_unredacted_reveal_uses_the_enum_and_names_the_files_tenant(monkeypatch):
    from app.api.endpoints.files import crud

    seen: list[dict] = []
    monkeypatch.setattr(audit_module.audit_logger, "log", lambda **kw: seen.append(kw))
    user = User(id=5, email="u@example.com")
    file_uuid = uuid.uuid4()

    for org_id, scope in ((9, "org"), (None, "personal")):
        media_file = MediaFile(id=1, uuid=file_uuid, organization_id=org_id)
        crud.audit_unredacted_reveal(media_file, user, {"email"}, surface="transcript")
        assert seen[-1]["event_type"] is AuditEventType.TRANSCRIPT_VIEW_UNREDACTED
        assert (seen[-1]["resource_type"], seen[-1]["resource_uuid"]) == (
            "media_file",
            str(file_uuid),
        )
        assert seen[-1]["tenant_scope"] == scope
        assert seen[-1]["organization_id"] == org_id
