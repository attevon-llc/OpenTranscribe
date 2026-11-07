"""The private media host allowlist: super-admin only, default-deny, never lifts the metadata block."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.auth.audit_events import AuditEventType
from app.schemas.media_source import UserMediaSourceCreate
from app.services import media_private_hosts

URL = "/api/admin/media-private-hosts"


class _Borrowed:
    """The test session, lent to code that opens its own ``SessionLocal()`` and closes it."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        """Closing is the test's job, not the borrower's."""


@pytest.fixture(autouse=True)
def _shared_session(monkeypatch, db_session):
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _Borrowed(db_session))


@pytest.fixture
def private_dns(monkeypatch):
    """Resolve the test names to private / metadata addresses without real DNS."""
    import socket

    table = {
        "media.lan": "10.0.0.5",
        "metadata.example": "169.254.169.254",
        "public.example": "93.184.216.34",
    }

    def fake(host, port, *args, **kwargs):
        if host in table:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port or 0))]
        raise socket.gaierror(host)

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _create(hostname: str):
    return UserMediaSourceCreate(hostname=hostname)


def test_private_host_is_refused_by_default_with_an_actionable_message(private_dns):
    with pytest.raises(ValueError, match="media_source_private_host"):
        _create("http://media.lan")


def test_private_host_is_accepted_once_a_super_admin_lists_it(
    private_dns, db_session, super_admin_token_headers, client
):
    resp = client.put(URL, json={"hosts": ["http://media.lan"]}, headers=super_admin_token_headers)
    assert resp.status_code == 200
    assert resp.json()["hosts"] == ["http://media.lan"]
    assert _create("http://media.lan").hostname == "http://media.lan"
    # A different scheme or port is a different origin.
    with pytest.raises(ValueError, match="media_source_private_host"):
        _create("https://media.lan")
    with pytest.raises(ValueError, match="media_source_private_host"):
        _create("http://media.lan:8080")


def test_listing_a_host_never_unblocks_cloud_metadata(private_dns, db_session):
    media_private_hosts.set_private_hosts(db_session, ["http://metadata.example"])
    with pytest.raises(ValueError, match="media_source_private_host"):
        _create("http://metadata.example")


def test_the_message_does_not_reveal_whether_the_name_resolves(private_dns):
    with pytest.raises(ValidationError) as private:
        _create("http://media.lan")
    with pytest.raises(ValidationError) as unresolvable:
        _create("http://nothing-here.example")
    assert private.value.errors()[0]["msg"] == unresolvable.value.errors()[0]["msg"]


def test_only_a_super_admin_can_change_the_list(client, admin_token_headers, user_token_headers):
    assert (
        client.put(URL, json={"hosts": ["a.lan"]}, headers=admin_token_headers).status_code == 403
    )
    assert client.get(URL, headers=user_token_headers).status_code == 403


def test_put_is_audited_and_malformed_entries_are_rejected(
    client, super_admin_token_headers, monkeypatch
):
    events = []
    monkeypatch.setattr(
        "app.api.endpoints.media_private_hosts.audit_logger.log", lambda **kw: events.append(kw)
    )
    ok = client.put(URL, json={"hosts": ["media.lan"]}, headers=super_admin_token_headers)
    assert ok.status_code == 200
    assert [e["event_type"] for e in events] == [AuditEventType.ADMIN_SETTINGS_CHANGE]
    bad = client.put(URL, json={"hosts": ["ftp://x.lan"]}, headers=super_admin_token_headers)
    assert bad.status_code == 422


def test_the_fetch_path_honours_the_same_list(private_dns, db_session):
    """Adding a host is not enough: the pinned fetch must also be allowed to dial it."""
    from app.core.exceptions import InvalidInputError
    from app.utils.media_origin import _pin_media_url

    with pytest.raises(InvalidInputError):
        _pin_media_url("http://media.lan/api/v1/login", purpose="test")
    media_private_hosts.set_private_hosts(db_session, ["http://media.lan"])
    assert _pin_media_url("http://media.lan/api/v1/login", purpose="test").address == "10.0.0.5"
    media_private_hosts.set_private_hosts(db_session, ["http://metadata.example"])
    with pytest.raises(InvalidInputError):
        _pin_media_url("http://metadata.example/latest", purpose="test")
