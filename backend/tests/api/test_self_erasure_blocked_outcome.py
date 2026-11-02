"""``GET /users/me/erasure-request`` reports a request the system closed without erasing.

A request blocked because it would remove the last active super administrator used to vanish
from the status: it is ``cancelled`` and ``get_open_request`` only returns open rows. The person
who asked then had no way to learn that nothing was erased.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core.constants import GDPR_SELF_ERASURE_ENABLED_KEY
from app.models.erasure import ErasureLedgerEntry
from app.services import erasure_ledger_service as ledger
from app.services.system_settings_service import set_setting

URL = "/api/users/me/erasure-request"
GOOD = {"confirm": "ERASE", "password": "password123"}


@pytest.fixture(autouse=True)
def journal_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)


@pytest.fixture()
def enabled(db_session):
    set_setting(db_session, GDPR_SELF_ERASURE_ENABLED_KEY, True)


def _entry(db_session, ledger_uuid: str) -> ErasureLedgerEntry:
    entry: ErasureLedgerEntry = (
        db_session.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.uuid == uuid_pkg.UUID(ledger_uuid))
        .one()
    )
    assert entry.status == "deferred"
    return entry


def test_status_reports_a_request_blocked_as_last_super_admin(
    client, user_token_headers, enabled, db_session
):
    receipt = client.post(URL, json=GOOD, headers=user_token_headers).json()
    entry = _entry(db_session, receipt["ledger_uuid"])
    ledger.block_request(db_session, entry, reason="last_super_admin")

    body = client.get(URL, headers=user_token_headers).json()

    assert body["request"] is None
    assert body["last_outcome"] == {
        "status": "blocked",
        "reason": "last_super_admin",
        "ledger_uuid": receipt["ledger_uuid"],
        "at": body["last_outcome"]["at"],
    }
    assert body["last_outcome"]["at"] is not None


def test_a_newer_request_hides_an_older_block(client, user_token_headers, enabled, db_session):
    first = client.post(URL, json=GOOD, headers=user_token_headers).json()
    ledger.block_request(
        db_session, _entry(db_session, first["ledger_uuid"]), reason="last_super_admin"
    )
    assert client.get(URL, headers=user_token_headers).json()["last_outcome"] is not None

    second = client.post(URL, json=GOOD, headers=user_token_headers)
    assert second.status_code == 201, second.text

    body = client.get(URL, headers=user_token_headers).json()
    assert body["request"]["ledger_uuid"] == second.json()["ledger_uuid"]
    assert body["last_outcome"] is None


def test_no_request_at_all_has_no_outcome(client, user_token_headers, enabled):
    body = client.get(URL, headers=user_token_headers).json()

    assert body["request"] is None
    assert body["last_outcome"] is None
