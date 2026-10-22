"""``/api/admin/takedown-notices`` and ``/api/takedown-notices/mine`` over HTTP (issue #1254).

The record is only worth having if it cannot drift from the takedown it describes, if the
owner can read why their file vanished without learning the internal basis, and if the
export cannot be turned into a spreadsheet attack by the free text it carries.
"""

from __future__ import annotations

import csv
import io
import json
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute

from app.auth.audit import AuditEventType
from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.takedown import TakedownNotification
from app.services import takedown_notification_service as notify_mod
from tests.fixtures.support_world import login_headers
from tests.fixtures.support_world import make_user

ADMIN = "/api/admin/takedown-notices"
FILES = "/api/admin/files"
MINE = "/api/takedown-notices/mine"
_FORMULA = ("=", "+", "-", "@", "\t", "\r", "\n")


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", MagicMock())
    monkeypatch.setattr("app.services.minio_service.set_object_quarantine_tag", MagicMock())
    monkeypatch.setattr("app.services.minio_service.set_object_legal_hold", MagicMock())


def _file(db, owner) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"tn_{file_uuid.hex[:8]}.wav",
        storage_path="",
        content_type="audio/wav",
        file_size=1,
        status=FileStatus.COMPLETED,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def _body(**over):
    body = {
        "notice_type": "dmca",
        "received_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
        "requester_reference": "CASE-42",
        "basis": "17 USC 512(c) notice, complete",
    }
    body.update(over)
    return body


def _make_notice(client, headers, **over):
    r = client.post(ADMIN, headers=headers, json=_body(**over))
    assert r.status_code == 201, r.text
    return r.json()


def _quarantine(client, headers, file, **over):
    body = {"reason": "Removed after a copyright notice"}
    body.update(over)
    return client.post(f"{FILES}/{file.uuid}/quarantine", headers=headers, json=body)


# --- privilege ---------------------------------------------------------------------------

# Fixed ids: parametrize ids must be identical on every xdist worker.
_NID, _FID = uuid_pkg.UUID(int=1), uuid_pkg.UUID(int=2)
_ROUTES = [
    ("post", ADMIN, {"json": _body()}),
    ("get", ADMIN, {}),
    ("get", f"{ADMIN}/export", {}),
    ("get", f"{ADMIN}/{_NID}", {}),
    ("patch", f"{ADMIN}/{_NID}/files/{_FID}", {"json": {"status": "upheld"}}),
    ("post", f"{ADMIN}/{_NID}/notify", {"json": {}}),
]


@pytest.mark.parametrize(("method", "path", "kwargs"), _ROUTES)
def test_a_plain_user_and_an_anonymous_caller_are_both_refused_with_403(
    client, user_token_headers, method, path, kwargs
):
    assert getattr(client, method)(path, headers=user_token_headers, **kwargs).status_code == 403
    assert getattr(client, method)(path, **kwargs).status_code == 403


def test_every_notice_route_depends_on_the_admin_gate():
    """Walk the live route table. An empty walk must not pass, so the set is pinned."""
    from app.api.endpoints.auth import get_current_admin_user
    from app.main import app

    def _calls(dep):
        yield dep.call
        for sub in dep.dependencies:
            yield from _calls(sub)

    found = {}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(ADMIN):
            found[(route.path, tuple(sorted(route.methods)))] = get_current_admin_user in set(
                _calls(route.dependant)
            )
    assert len(found) == 6, sorted(found)
    assert all(found.values()), [k for k, ok in found.items() if not ok]


# --- recording ---------------------------------------------------------------------------


def test_creating_a_notice_changes_no_file_and_is_audited(
    client, admin_token_headers, normal_user, db_session, monkeypatch
):
    fake = MagicMock()
    monkeypatch.setattr("app.api.endpoints.admin_takedown_notices.audit_logger", fake)
    file = _file(db_session, normal_user)

    notice = _make_notice(client, admin_token_headers)

    assert notice["links"] == [] and notice["status"] == "open"
    assert notice["recorded_by_authority"] == "platform"
    db_session.refresh(file)
    assert file.is_quarantined is False
    assert fake.log.call_args.kwargs["event_type"] == AuditEventType.ADMIN_TAKEDOWN_NOTICE_CREATE
    assert "basis" not in json.dumps(fake.log.call_args.kwargs["details"])


@pytest.mark.parametrize(
    "over",
    [
        {"received_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        {"received_at": "2026-01-01T00:00:00"},
        {"notice_type": "operator_action"},
        {"notice_type": "nonsense"},
        {"requester_reference": "   "},
        {"requester_reference": None},
        {"basis": ""},
        {"basis": "x" * 2001},
    ],
)
def test_a_malformed_notice_is_refused_and_nothing_is_stored(
    client, admin_token_headers, over, db_session
):
    before = client.get(ADMIN, headers=admin_token_headers).json()["total"]
    r = client.post(ADMIN, headers=admin_token_headers, json=_body(**over))
    assert r.status_code == 422
    assert client.get(ADMIN, headers=admin_token_headers).json()["total"] == before


def test_quarantine_through_the_route_links_the_file_to_the_cited_notice(
    client, admin_token_headers, normal_user, db_session
):
    file = _file(db_session, normal_user)
    notice = _make_notice(client, admin_token_headers)

    r = _quarantine(client, admin_token_headers, file, notice_uuid=notice["uuid"])

    assert r.status_code == 200, r.text
    detail = client.get(f"{ADMIN}/{notice['uuid']}", headers=admin_token_headers).json()
    (link,) = detail["links"]
    assert link["file_uuid"] == str(file.uuid) and link["status"] == "open"
    assert link["owner_reason"] == "Removed after a copyright notice"
    assert link["file_exists"] is True and link["file_title"] == file.filename
    queue = client.get(f"{FILES}/quarantined", headers=admin_token_headers).json()["files"]
    row = next(f for f in queue if f["uuid"] == str(file.uuid))
    assert [(n["notice_uuid"], n["status"]) for n in row["notices"]] == [(notice["uuid"], "open")]


def test_an_operator_action_is_recorded_as_a_notice_with_the_stated_reason(
    client, admin_token_headers, normal_user, db_session
):
    file = _file(db_session, normal_user)
    r = _quarantine(
        client, admin_token_headers, file, operator_action_reason="Spotted in a routine review"
    )
    assert r.status_code == 200, r.text
    notices = client.get(ADMIN, headers=admin_token_headers, params={"file_uuid": str(file.uuid)})
    (n,) = notices.json()["notices"]
    assert (n["notice_type"], n["requester_reference"]) == ("operator_action", None)
    assert n["basis"] == "Spotted in a routine review"


@pytest.mark.parametrize(
    ("make_body", "expected"),
    [
        (lambda n: {}, 422),
        (lambda n: {"notice_uuid": n, "operator_action_reason": "both"}, 422),
        (lambda n: {"operator_action_reason": "   "}, 422),
        (lambda n: {"notice_uuid": str(uuid_pkg.uuid4())}, 404),
        (lambda n: {"notice_uuid": "not-a-uuid"}, 422),
    ],
)
def test_the_wire_contract_refuses_a_takedown_with_no_valid_basis(
    client, admin_token_headers, normal_user, db_session, make_body, expected
):
    file = _file(db_session, normal_user)
    notice = _make_notice(client, admin_token_headers)
    r = _quarantine(client, admin_token_headers, file, **make_body(notice["uuid"]))
    assert r.status_code == expected, r.text
    db_session.refresh(file)
    assert file.is_quarantined is False and file.legal_hold is False


def test_release_closes_the_link_and_keeps_what_the_owner_was_shown(
    client, admin_token_headers, normal_user, db_session
):
    file = _file(db_session, normal_user)
    notice = _make_notice(client, admin_token_headers)
    _quarantine(client, admin_token_headers, file, notice_uuid=notice["uuid"])

    r = client.post(
        f"{FILES}/{file.uuid}/release",
        headers=admin_token_headers,
        json={"reason": "Complainant withdrew"},
    )

    assert r.status_code == 200, r.text
    detail = client.get(f"{ADMIN}/{notice['uuid']}", headers=admin_token_headers).json()
    (link,) = detail["links"]
    assert link["status"] == "released" and link["outcome_note"] == "Complainant withdrew"
    assert link["owner_reason"] == "Removed after a copyright notice"
    assert detail["status"] == "resolved"


# --- outcomes ----------------------------------------------------------------------------


def test_outcomes_are_recorded_without_touching_the_file(
    client, admin_token_headers, normal_user, db_session
):
    file = _file(db_session, normal_user)
    notice = _make_notice(client, admin_token_headers)
    _quarantine(client, admin_token_headers, file, notice_uuid=notice["uuid"])
    url = f"{ADMIN}/{notice['uuid']}/files/{file.uuid}"

    counter = client.patch(url, headers=admin_token_headers, json={"status": "counter_notice"})
    assert counter.status_code == 200, counter.text
    (link,) = counter.json()["links"]
    assert link["status"] == "counter_notice" and link["counter_notice_received_at"]
    upheld = client.patch(url, headers=admin_token_headers, json={"status": "upheld", "note": "n"})
    assert upheld.json()["links"][0]["status"] == "upheld"
    assert upheld.json()["status"] == "resolved"
    db_session.refresh(file)
    assert file.is_quarantined is True, "recording an outcome never restores the file"


@pytest.mark.parametrize("status", ["released", "open", "bogus"])
def test_released_can_only_come_from_the_release_action(
    client, admin_token_headers, normal_user, db_session, status
):
    file = _file(db_session, normal_user)
    notice = _make_notice(client, admin_token_headers)
    _quarantine(client, admin_token_headers, file, notice_uuid=notice["uuid"])
    r = client.patch(
        f"{ADMIN}/{notice['uuid']}/files/{file.uuid}",
        headers=admin_token_headers,
        json={"status": status},
    )
    assert r.status_code == 422
    db_session.refresh(file)
    assert file.is_quarantined is True


def test_an_outcome_on_a_released_file_or_an_unknown_file_is_refused(
    client, admin_token_headers, normal_user, db_session
):
    file = _file(db_session, normal_user)
    notice = _make_notice(client, admin_token_headers)
    _quarantine(client, admin_token_headers, file, notice_uuid=notice["uuid"])
    client.post(f"{FILES}/{file.uuid}/release", headers=admin_token_headers, json={})

    closed = client.patch(
        f"{ADMIN}/{notice['uuid']}/files/{file.uuid}",
        headers=admin_token_headers,
        json={"status": "upheld"},
    )
    assert (
        closed.status_code == 409
        and closed.json()["detail"]["code"] == "takedown_notice_link_closed"
    )
    unknown = client.patch(
        f"{ADMIN}/{notice['uuid']}/files/{uuid_pkg.uuid4()}",
        headers=admin_token_headers,
        json={"status": "upheld"},
    )
    assert unknown.status_code == 404


def test_the_list_filters_by_derived_status_type_and_file(
    client, admin_token_headers, normal_user, db_session
):
    file = _file(db_session, normal_user)
    open_notice = _make_notice(client, admin_token_headers)
    done = _make_notice(client, admin_token_headers, notice_type="court_order")
    _quarantine(client, admin_token_headers, file, notice_uuid=done["uuid"])
    client.patch(
        f"{ADMIN}/{done['uuid']}/files/{file.uuid}",
        headers=admin_token_headers,
        json={"status": "upheld"},
    )

    def uuids(**params):
        r = client.get(ADMIN, headers=admin_token_headers, params=params)
        assert r.status_code == 200, r.text
        return {n["uuid"] for n in r.json()["notices"]}

    assert open_notice["uuid"] in uuids(status="open")
    assert done["uuid"] not in uuids(status="open")
    assert done["uuid"] in uuids(status="resolved")
    assert uuids(notice_type="court_order", status="resolved") >= {done["uuid"]}
    assert uuids(file_uuid=str(file.uuid)) == {done["uuid"]}


# --- export ------------------------------------------------------------------------------


def _hostile_world(client, headers, db_session, owner):
    """One file whose every free-text field starts with a formula character."""
    file = _file(db_session, owner)
    notice = _make_notice(
        client,
        headers,
        requester_reference='=HYPERLINK("http://x","y")',
        basis="@SUM(1+1)",
    )
    _quarantine(client, headers, file, notice_uuid=notice["uuid"], reason="+cmd|' /C calc'!A0")
    client.patch(
        f"{ADMIN}/{notice['uuid']}/files/{file.uuid}",
        headers=headers,
        json={"status": "upheld", "note": "-2+3"},
    )
    return file, notice


def test_csv_export_neutralises_every_cell_of_every_column(
    client, admin_token_headers, normal_user, db_session
):
    file, notice = _hostile_world(client, admin_token_headers, db_session, normal_user)

    r = client.get(f"{ADMIN}/export", headers=admin_token_headers, params={"format": "csv"})

    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    header, body = rows[0], rows[1:]
    mine = [
        dict(zip(header, row, strict=True))
        for row in body
        if row[header.index("file_uuid")] == str(file.uuid)
    ]
    assert len(mine) == 1
    record = mine[0]
    assert record["requester_reference"] == '\'=HYPERLINK("http://x","y")'
    assert record["basis"] == "'@SUM(1+1)"
    assert record["owner_reason"] == "'+cmd|' /C calc'!A0"
    assert record["outcome_note"] == "'-2+3"
    assert len(body) >= 1
    for row in rows:
        assert len(row) == len(header)
        assert not [c for c in row if c.startswith(_FORMULA)], row
    disposition = r.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="takedown-notices-')
    assert str(notice["uuid"]) not in disposition


def test_json_export_has_the_same_rows_and_works_without_opensearch(
    client, admin_token_headers, normal_user, db_session, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AUDIT_LOG_TO_OPENSEARCH", False)
    file, _notice = _hostile_world(client, admin_token_headers, db_session, normal_user)
    r = client.get(f"{ADMIN}/export", headers=admin_token_headers, params={"format": "json"})
    assert r.status_code == 200
    (row,) = [x for x in r.json() if x["file_uuid"] == str(file.uuid)]
    assert row["status"] == "upheld" and row["notice_type"] == "dmca"
    assert r.headers["x-export-truncated"] == "false"


def test_an_export_over_the_cap_says_it_is_partial_and_is_audited(
    client, admin_token_headers, normal_user, db_session, monkeypatch
):
    fake = MagicMock()
    monkeypatch.setattr("app.api.endpoints.admin_takedown_notices.audit_logger", fake)
    monkeypatch.setattr("app.api.endpoints.admin_takedown_notices.TAKEDOWN_EXPORT_MAX_ROWS", 1)
    notice = _make_notice(client, admin_token_headers)
    for _ in range(2):
        _quarantine(
            client, admin_token_headers, _file(db_session, normal_user), notice_uuid=notice["uuid"]
        )

    r = client.get(f"{ADMIN}/export", headers=admin_token_headers, params={"format": "json"})

    assert len(r.json()) == 1 and r.headers["x-export-truncated"] == "true"
    event = next(
        c.kwargs
        for c in fake.log.call_args_list
        if c.kwargs["event_type"] == AuditEventType.ADMIN_TAKEDOWN_NOTICE_EXPORT
    )
    assert event["details"]["rows"] == 1 and event["details"]["truncated"] is True


def test_the_export_range_filters_on_received_at(
    client, admin_token_headers, normal_user, db_session
):
    old = _make_notice(client, admin_token_headers, received_at="2020-01-01T00:00:00+00:00")
    _quarantine(
        client, admin_token_headers, _file(db_session, normal_user), notice_uuid=old["uuid"]
    )
    rows = client.get(
        f"{ADMIN}/export",
        headers=admin_token_headers,
        params={"format": "json", "to": "2021-01-01T00:00:00+00:00"},
    ).json()
    assert {r["notice_uuid"] for r in rows} == {old["uuid"]}


# --- the owner's view --------------------------------------------------------------------


def test_the_owner_sees_their_own_takedowns_and_never_the_internal_record(
    client, admin_token_headers, db_session, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ABUSE_CONTACT_EMAIL", "abuse@operator.example")
    alice = make_user(db_session, role="user", label="alice")
    bob = make_user(db_session, role="user", label="bob")
    a_file, b_file = _file(db_session, alice), _file(db_session, bob)
    notice = _make_notice(client, admin_token_headers)
    _quarantine(client, admin_token_headers, a_file, notice_uuid=notice["uuid"])
    _quarantine(
        client,
        admin_token_headers,
        b_file,
        operator_action_reason="internal only",
        reason="Bob's reason",
    )

    alice_view = client.get(MINE, headers=login_headers(client, alice))
    assert alice_view.status_code == 200, alice_view.text
    body = alice_view.json()
    (item,) = body["takedowns"]
    assert body["total"] == 1
    assert item["file_uuid"] == str(a_file.uuid) and item["status"] == "open"
    assert item["owner_reason"] == "Removed after a copyright notice"
    assert item["dispute_to"] == "operator"
    assert item["abuse_contact_email"] == "abuse@operator.example"
    raw = alice_view.text
    for secret in ("CASE-42", "17 USC 512", "internal only", str(b_file.uuid), "Bob's reason"):
        assert secret not in raw
    for forbidden in ("requester_reference", "basis", "handled_by", "owner_user_uuid"):
        assert forbidden not in raw


def test_no_parameter_changes_whose_takedowns_the_owner_route_returns(
    client, admin_token_headers, db_session
):
    alice = make_user(db_session, role="user", label="alice2")
    bob = make_user(db_session, role="user", label="bob2")
    _quarantine(client, admin_token_headers, _file(db_session, bob), operator_action_reason="x")
    params = {"user_uuid": str(bob.uuid), "owner_user_uuid": str(bob.uuid), "user_id": bob.id}
    r = client.get(MINE, headers=login_headers(client, alice), params=params)
    assert r.status_code == 200 and r.json() == {"takedowns": [], "total": 0}


def test_the_owner_route_requires_a_session(client):
    assert client.get(MINE).status_code == 401


def test_an_owner_with_no_takedowns_gets_an_empty_list_and_a_released_file_stays_listed(
    client, admin_token_headers, db_session
):
    carol = make_user(db_session, role="user", label="carol")
    headers = login_headers(client, carol)
    assert client.get(MINE, headers=headers).json()["total"] == 0
    f = _file(db_session, carol)
    _quarantine(client, admin_token_headers, f, operator_action_reason="x")
    client.post(f"{FILES}/{f.uuid}/release", headers=admin_token_headers, json={})
    (item,) = client.get(MINE, headers=headers).json()["takedowns"]
    assert item["status"] == "released" and item["outcome_at"]


# --- notification ------------------------------------------------------------------------


class _Mailer:
    """A stand-in transport recording what the notify path hands it."""

    def __init__(self, *, available=True, fail=False):
        self.available, self.fail, self.sent = available, fail, []

    def has_transport(self):
        return self.available

    def send_takedown_notice(self, to_email, subject, heading, paragraphs):
        if self.fail:
            raise RuntimeError(f"smtp exploded for {to_email}")
        self.sent.append((to_email, subject, heading, paragraphs))


@pytest.fixture
def mailer(monkeypatch):
    m = _Mailer()
    monkeypatch.setattr(notify_mod, "email_service", m)
    return m


def _notified_notice(client, headers, db_session, owner, n=3):
    notice = _make_notice(client, headers)
    files = [_file(db_session, owner) for _ in range(n)]
    for f in files:
        assert _quarantine(client, headers, f, notice_uuid=notice["uuid"]).status_code == 200
    return notice, files


def test_one_email_per_owner_per_notice_however_many_files_and_a_repeat_sends_none(
    client, admin_token_headers, normal_user, db_session, mailer
):
    notice, _files = _notified_notice(client, admin_token_headers, db_session, normal_user)
    url = f"{ADMIN}/{notice['uuid']}/notify"

    first = client.post(url, headers=admin_token_headers, json={})
    again = client.post(url, headers=admin_token_headers, json={})

    assert first.json() == {
        "sent": 1,
        "failed": 0,
        "skipped_no_transport": 0,
        "already_notified": 0,
    }
    assert again.json() == {
        "sent": 0,
        "failed": 0,
        "skipped_no_transport": 0,
        "already_notified": 3,
    }
    assert len(mailer.sent) == 1
    to, _subject, _heading, paragraphs = mailer.sent[0]
    assert to == normal_user.email
    assert sum("Removed after a copyright notice" in p for p in paragraphs) == 3


def test_the_notify_request_cannot_name_a_recipient(
    client, admin_token_headers, normal_user, db_session, mailer
):
    notice, _ = _notified_notice(client, admin_token_headers, db_session, normal_user, n=1)
    r = client.post(
        f"{ADMIN}/{notice['uuid']}/notify",
        headers=admin_token_headers,
        json={"event": "quarantined", "to": "attacker@example.com", "user_ids": [1]},
    )
    assert r.status_code == 200
    assert [m[0] for m in mailer.sent] == [normal_user.email]


def test_without_a_transport_nothing_is_sent_it_is_recorded_and_a_later_call_delivers(
    client, admin_token_headers, normal_user, db_session, mailer
):
    notice, _files = _notified_notice(client, admin_token_headers, db_session, normal_user, n=2)
    url = f"{ADMIN}/{notice['uuid']}/notify"
    mailer.available = False

    skipped = client.post(url, headers=admin_token_headers, json={}).json()
    assert skipped == {"sent": 0, "failed": 0, "skipped_no_transport": 1, "already_notified": 0}
    assert mailer.sent == []
    statuses = {r.status for r in db_session.query(TakedownNotification)}
    assert statuses == {"skipped_no_transport"}

    mailer.available = True
    delivered = client.post(url, headers=admin_token_headers, json={}).json()
    assert delivered["sent"] == 1 and len(mailer.sent) == 1
    assert {r.status for r in db_session.query(TakedownNotification)} == {"sent"}


def test_a_failing_transport_is_counted_never_raised_and_never_undoes_the_takedown(
    client, admin_token_headers, normal_user, db_session, mailer, caplog
):
    notice, (file,) = _notified_notice(client, admin_token_headers, db_session, normal_user, n=1)
    mailer.fail = True
    caplog.clear()

    r = client.post(f"{ADMIN}/{notice['uuid']}/notify", headers=admin_token_headers, json={})

    assert r.status_code == 200 and r.json()["failed"] == 1
    db_session.refresh(file)
    assert file.is_quarantined is True
    (row,) = db_session.query(TakedownNotification).all()
    assert row.status == "failed"
    assert normal_user.email not in caplog.text, "an address must not reach the log"
    assert "Removed after a copyright notice" not in caplog.text


def test_a_release_can_be_announced_and_is_idempotent_too(
    client, admin_token_headers, normal_user, db_session, mailer
):
    notice, (file,) = _notified_notice(client, admin_token_headers, db_session, normal_user, n=1)
    url = f"{ADMIN}/{notice['uuid']}/notify"
    assert client.post(url, headers=admin_token_headers, json={}).json()["sent"] == 1
    client.post(f"{FILES}/{file.uuid}/release", headers=admin_token_headers, json={})

    released = client.post(url, headers=admin_token_headers, json={"event": "released"})
    again = client.post(url, headers=admin_token_headers, json={"event": "released"})

    assert released.json()["sent"] == 1 and again.json()["sent"] == 0
    assert mailer.sent[-1][1] == "Access to your files was restored"
    assert len(mailer.sent) == 2


def test_an_owner_whose_account_is_gone_is_skipped_not_mailed(
    client, admin_token_headers, db_session, mailer
):
    gone = make_user(db_session, role="user", label="gone")
    notice, _ = _notified_notice(client, admin_token_headers, db_session, gone, n=1)
    gone.is_active = False
    db_session.commit()

    r = client.post(f"{ADMIN}/{notice['uuid']}/notify", headers=admin_token_headers, json={})

    assert r.json()["sent"] == 0 and mailer.sent == []
    assert {x.status for x in db_session.query(TakedownNotification)} == {"skipped_no_recipient"}
