"""A media source is unique per origin (scheme, host, effective port), not per spelling.

Legacy rows carry no scheme and new ones are stored as ``https://host``, so a string
comparison would accept the same server twice. ``http://`` on the same host is a different
origin and stays allowed.
"""

import uuid

import pytest

from app.models.user_media_source import UserMediaSource
from tests.helpers import stub_public_dns

USER_PATH = "/api/user-settings/media-sources"
ADMIN_PATH = "/api/admin/settings/media-sources"


@pytest.fixture(autouse=True)
def _public_media_host_dns(monkeypatch):
    stub_public_dns(monkeypatch)


def _legacy_row(db, user, hostname):
    row = UserMediaSource(
        uuid=uuid.uuid4(),
        user_id=user.id,
        hostname=hostname,
        provider_type="mediacms",
        verify_ssl=True,
        is_active=True,
        is_shared=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.mark.parametrize(
    "given", ["dupo.example.com", "https://dupo.example.com", "HTTPS://Dupo.Example.com:443"]
)
def test_user_source_with_the_origin_of_a_legacy_row_is_refused(
    client, db_session, normal_user, user_token_headers, given
):
    _legacy_row(db_session, normal_user, "dupo.example.com")

    resp = client.post(USER_PATH, json={"hostname": given}, headers=user_token_headers)

    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]


def test_user_source_over_http_is_a_different_origin(
    client, db_session, normal_user, user_token_headers
):
    _legacy_row(db_session, normal_user, "plaino.example.com")

    resp = client.post(
        USER_PATH, json={"hostname": "http://plaino.example.com"}, headers=user_token_headers
    )

    assert resp.status_code == 200
    assert resp.json()["hostname"] == "http://plaino.example.com"


def test_user_source_edited_onto_another_rows_origin_is_refused(
    client, db_session, normal_user, user_token_headers
):
    _legacy_row(db_session, normal_user, "taken.example.com")
    other = _legacy_row(db_session, normal_user, "free.example.com")

    resp = client.put(
        f"{USER_PATH}/{other.uuid}",
        json={"hostname": "https://taken.example.com:443"},
        headers=user_token_headers,
    )

    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]


def test_user_source_renamed_to_its_own_origin_is_allowed(
    client, db_session, normal_user, user_token_headers
):
    own = _legacy_row(db_session, normal_user, "self.example.com")

    resp = client.put(
        f"{USER_PATH}/{own.uuid}",
        json={"hostname": "https://self.example.com"},
        headers=user_token_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["hostname"] == "https://self.example.com"


def test_admin_system_source_is_unique_per_origin(client, admin_token_headers):
    first = client.post(
        ADMIN_PATH, json={"hostname": "sysdup.example.com"}, headers=admin_token_headers
    )
    assert first.status_code == 200, first.text

    for same in ("https://sysdup.example.com", "https://SysDup.example.com:443"):
        resp = client.post(ADMIN_PATH, json={"hostname": same}, headers=admin_token_headers)
        assert resp.status_code == 400, same
        assert "already exists" in resp.json()["detail"]

    plain = client.post(
        ADMIN_PATH, json={"hostname": "http://sysdup.example.com"}, headers=admin_token_headers
    )
    assert plain.status_code == 200, plain.text

    moved = client.put(
        f"{ADMIN_PATH}/{plain.json()['id']}",
        json={"hostname": "sysdup.example.com:443"},
        headers=admin_token_headers,
    )
    assert moved.status_code == 400
    assert "already exists" in moved.json()["detail"]
