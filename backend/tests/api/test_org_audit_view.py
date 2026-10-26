"""The organization admin's audit read and export, over HTTP (#1277).

``GET /api/org-admin/audit-logs`` gains resource and uuid filters and resolved display names;
``GET /api/org-admin/audit-logs/export`` is new and sits behind its own ``org.audit_logs``
capability. ``query_audit_logs`` is replaced by a recorder here (the real clause against a real
cluster is ``tests/integration/test_org_audit_export_opensearch.py``), so what these tests pin
is everything AROUND the query: who may call, what scope and filters reach it, what is added to
the result, what the file contains and that the export audits itself.
"""

from __future__ import annotations

import csv
import io
import json
import uuid as uuid_pkg

import pytest
from fastapi import status

from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver

LIST = "/api/org-admin/audit-logs"
EXPORT = "/api/org-admin/audit-logs/export"


def _capabilities(**keys: bool):
    set_capability_resolver(lambda _request: {**COMMUNITY_CAPABILITIES, **keys})


@pytest.fixture()
def caps_on():
    _capabilities(organizations=True, **{"org.audit_logs": True})
    yield
    reset_capability_resolver()


@pytest.fixture()
def read_only_caps():
    """The read is on, the export capability is off: the community and a typical cloud default."""
    _capabilities(organizations=True, **{"org.audit_logs": False})
    yield
    reset_capability_resolver()


class _Queries(list):
    """The recorded ``query_audit_logs`` calls, plus the result the next call serves."""

    def __init__(self):
        super().__init__()
        self.logs: list[dict] = []
        self.total: int | None = None


@pytest.fixture()
def queries(monkeypatch) -> _Queries:
    """Record the arguments of every ``query_audit_logs`` call and serve ``queries.logs``."""
    calls = _Queries()

    def _fake(**kwargs):
        calls.append(kwargs)
        total = calls.total if calls.total is not None else len(calls.logs)
        return {"logs": calls.logs, "total": total, "offset": 0, "limit": kwargs["limit"]}

    for module in ("org_admin", "org_admin_audit"):
        monkeypatch.setattr(f"app.api.endpoints.{module}.query_audit_logs", _fake)
    return calls


@pytest.fixture()
def exported_events(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))
    return written


def _serve(queries: _Queries, logs: list[dict], total: int | None = None) -> None:
    queries.logs = logs
    queries.total = total


def _event(**fields) -> dict:
    return {
        "timestamp": "2026-10-25T12:00:00Z",
        "event_type": "content.file.view",
        "outcome": "success",
        **fields,
    }


# --------------------------------------------------------------------------- #
# Display names: resolved inside the organization only
# --------------------------------------------------------------------------- #


def test_events_gain_names_only_for_members_and_titles_only_for_org_resources(
    client, gov, queries, caps_on
):
    org_file = gov.file(gov.alice, title="Retail review")
    other_file = gov.file(gov.outsider, org=gov.other_org, title="Other tenant secret")
    org_collection = gov.collection(gov.alice, name="Matter 7")
    _serve(
        queries,
        [
            _event(
                user_id=gov.bob.id,
                target_user_id=gov.alice.id,
                resource_type="media_file",
                resource_uuid=str(org_file.uuid),
            ),
            _event(
                user_id=gov.outsider.id,
                resource_type="media_file",
                resource_uuid=str(other_file.uuid),
            ),
            _event(resource_type="collection", resource_uuid=str(org_collection.uuid)),
            _event(resource_type="media_file", resource_uuid=str(uuid_pkg.uuid4())),
        ],
    )
    with gov.acting_as(gov.admin):
        response = client.get(LIST)
    assert response.status_code == status.HTTP_200_OK, response.text

    mine, foreign, collection, deleted = response.json()["logs"]
    assert mine["actor_display"] == gov.bob.full_name
    assert mine["target_display"] == gov.alice.full_name
    assert mine["resource_display"] == "Retail review"
    # The control above proves labels resolve; these prove they do not cross the tenant line.
    assert foreign["actor_display"] is None
    assert foreign["resource_display"] is None
    assert collection["resource_display"] == "Matter 7"
    assert deleted["resource_display"] is None
    assert "Other tenant secret" not in response.text


def test_a_member_who_left_is_an_id_not_a_name(client, gov, queries, caps_on):
    from app.models.organization import OrganizationMembership

    gov.db.query(OrganizationMembership).filter_by(user_id=gov.bob.id).delete()
    gov.db.commit()
    _serve(queries, [_event(user_id=gov.bob.id)])
    with gov.acting_as(gov.admin):
        logs = client.get(LIST).json()["logs"]
    assert logs[0]["user_id"] == gov.bob.id
    assert logs[0]["actor_display"] is None


# --------------------------------------------------------------------------- #
# Filters and the member guard
# --------------------------------------------------------------------------- #


def test_resource_and_uuid_filters_reach_the_query_inside_the_org_scope(
    client, gov, queries, caps_on
):
    file_uuid = uuid_pkg.uuid4()
    with gov.acting_as(gov.admin):
        response = client.get(
            LIST,
            params={
                "resource_type": "media_file",
                "resource_uuid": str(file_uuid),
                "user_uuid": str(gov.bob.uuid),
                "target_user_uuid": str(gov.alice.uuid),
            },
        )
    assert response.status_code == status.HTTP_200_OK, response.text
    sent = queries[0]
    assert sent["resource_type"] == "media_file"
    assert sent["resource_uuid"] == str(file_uuid)
    assert sent["user_id"] == gov.bob.id
    assert sent["target_user_id"] == gov.alice.id
    assert sent["scope_org_id"] == gov.org.id


@pytest.mark.parametrize("param", ["user_uuid", "target_user_uuid"])
def test_a_uuid_filter_naming_a_non_member_is_refused_before_any_query(
    client, gov, queries, caps_on, param
):
    with gov.acting_as(gov.admin):
        outsider = client.get(LIST, params={param: str(gov.outsider.uuid)})
        unknown = client.get(LIST, params={param: str(uuid_pkg.uuid4())})
        member = client.get(LIST, params={param: str(gov.bob.uuid)})
    assert outsider.status_code == status.HTTP_403_FORBIDDEN
    assert unknown.status_code == status.HTTP_403_FORBIDDEN
    # An unknown uuid and a real non-member answer identically: no oracle for who exists.
    assert outsider.json() == unknown.json()
    assert member.status_code == status.HTTP_200_OK
    assert len(queries) == 1, "only the member's request may reach the query"


# --------------------------------------------------------------------------- #
# The export
# --------------------------------------------------------------------------- #


def test_a_plain_member_cannot_export(client, gov, queries, caps_on):
    with gov.acting_as(gov.bob):
        response = client.get(EXPORT)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert queries == []


def test_the_export_is_a_404_while_the_capability_is_off_though_the_read_works(
    client, gov, queries, read_only_caps
):
    with gov.acting_as(gov.admin):
        assert client.get(LIST).status_code == status.HTTP_200_OK
        assert client.get(EXPORT).status_code == status.HTTP_404_NOT_FOUND
    assert len(queries) == 1, "the refused export must not have queried"


def test_a_platform_superuser_does_not_bypass_the_export_capability(
    client, gov, queries, read_only_caps, db_session
):
    gov.admin.role = "super_admin"
    gov.admin.is_superuser = True
    db_session.commit()
    with gov.acting_as(gov.admin):
        response = client.get(EXPORT)
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_a_csv_export_has_the_columns_the_names_and_the_truncation_header(
    client, gov, queries, caps_on, exported_events
):
    org_file = gov.file(gov.alice, title="Retail review")
    _serve(
        queries,
        [
            _event(
                user_id=gov.bob.id,
                resource_type="media_file",
                resource_uuid=str(org_file.uuid),
                organization_id=gov.org.id,
                tenant_scope="org",
                details={"surface": "detail"},
            )
        ],
        total=10_050,
    )
    with gov.acting_as(gov.admin):
        response = client.get(EXPORT, params={"format": "csv", "event_type": "content.file.view"})
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["x-export-truncated"] == "true"
    assert 'filename="audit-logs-' in response.headers["content-disposition"]

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    assert rows[0]["event_type"] == "content.file.view"
    assert rows[0]["resource_uuid"] == str(org_file.uuid)
    assert rows[0]["tenant_scope"] == "org"
    assert rows[0]["actor_display"] == gov.bob.full_name
    assert rows[0]["resource_display"] == "Retail review"
    assert json.loads(rows[0]["details"]) == {"surface": "detail"}
    sent = queries[0]
    assert sent["limit"] == 10_000 and sent["offset"] == 0
    assert sent["scope_org_id"] == gov.org.id
    assert sent["event_type"] == "content.file.view"


def test_a_complete_export_says_it_is_not_truncated(client, gov, queries, caps_on):
    _serve(queries, [_event(user_id=gov.bob.id)])
    with gov.acting_as(gov.admin):
        response = client.get(EXPORT)
    assert response.headers["x-export-truncated"] == "false"


def test_an_empty_export_still_has_a_header_row(client, gov, queries, caps_on):
    with gov.acting_as(gov.admin):
        response = client.get(EXPORT)
    assert response.status_code == status.HTTP_200_OK
    assert response.text.splitlines()[0].startswith("timestamp,event_type,outcome")


def test_csv_cells_that_start_like_a_formula_are_neutralised(client, gov, queries, caps_on):
    _serve(queries, [_event(username='=HYPERLINK("http://evil")', user_agent="+cmd|' /C calc'!A0")])
    with gov.acting_as(gov.admin):
        response = client.get(EXPORT)
    row = next(csv.DictReader(io.StringIO(response.text)))
    assert row["username"].startswith("'=")
    assert row["user_agent"].startswith("'+")


def test_the_json_export_is_the_enriched_event_list(client, gov, queries, caps_on):
    _serve(queries, [_event(user_id=gov.bob.id)])
    with gov.acting_as(gov.admin):
        response = client.get(EXPORT, params={"format": "json"})
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body[0]["actor_display"] == gov.bob.full_name


def test_an_unknown_format_is_a_422(client, gov, queries, caps_on):
    with gov.acting_as(gov.admin):
        assert client.get(EXPORT, params={"format": "xml"}).status_code == 422
    assert queries == []


def test_an_export_names_a_non_member_as_403_before_reading(client, gov, queries, caps_on):
    with gov.acting_as(gov.admin):
        response = client.get(EXPORT, params={"user_uuid": str(gov.outsider.uuid)})
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert queries == []


def test_an_export_is_itself_audited_without_the_rows(
    client, gov, queries, caps_on, exported_events
):
    _serve(queries, [_event(user_id=gov.bob.id, details={"secret": "do-not-copy"})])
    with gov.acting_as(gov.admin):
        assert client.get(EXPORT, params={"format": "json"}).status_code == status.HTTP_200_OK

    written = [e for e in exported_events if e["event_type"] is AuditEventType.AUDIT_EXPORT]
    assert len(written) == 1
    event = written[0]
    assert event["user_id"] == gov.admin.id
    assert event["organization_id"] == gov.org.id
    assert event["tenant_scope"] == "org"
    assert event["details"] == {
        "scope": "organization",
        "format": "json",
        "rows": 1,
        "truncated": False,
    }
    assert "do-not-copy" not in json.dumps(event, default=str)


def test_a_failed_query_is_a_503_that_echoes_nothing(client, gov, monkeypatch, caps_on):
    monkeypatch.setattr(
        "app.api.endpoints.org_admin_audit.query_audit_logs",
        lambda **kw: {"error": "connection to 10.0.0.5:9200 refused", "logs": [], "total": 0},
    )
    with gov.acting_as(gov.admin):
        response = client.get(EXPORT)
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "10.0.0.5" not in response.text
