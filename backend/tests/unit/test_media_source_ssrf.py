"""SSRF guards on user-configured MediaCMS media sources (audit finding A1).

``schemas/media_source.py`` carried a second "is this host safe" implementation beside
the canonical ``utils/url_validation``: a hostname regex, a "must contain a dot" rule,
and a hardcoded first-label blocklist. It never resolved DNS and never inspected an
address, so **nine of nine** hostile hosts passed it — ``169.254.169.254`` (cloud
instance metadata), ``127.0.0.1``, ``10.0.0.5``, ``0.0.0.0``,
``metadata.google.internal`` — while the canonical guard refused every one.

That value is not inert. It becomes a stored *allowed host*
(``protected_media_plugins/mediacms.py::_get_allowed_hosts``) which the provider then
fetches server-side with the user's credentials, surfacing response and error
differences back to the caller — a non-blind SSRF oracle reachable by any authenticated
non-admin user through ``POST /api/user-settings/media-sources``.

Two layers are pinned here because one is not enough. The schema refuses the value at
write time, and the provider re-checks immediately before every outbound request:
hostile rows stored before the fix still exist, and a hostname's DNS can change between
validation and use.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.media_source import UserMediaSourceCreate
from app.schemas.media_source import UserMediaSourceUpdate
from app.services.protected_media_plugins.mediacms import MediacmsProvider
from tests.helpers import stub_public_dns

# ---------------------------------------------------------------------------
# Schema-level guard
# ---------------------------------------------------------------------------


def test_media_source_hostname_rejects_link_local():
    """The headline A1 case: cloud instance metadata was an accepted media source.

    ``169.254.169.254`` answers with IAM/instance credentials on AWS, Azure, GCP,
    DigitalOcean, Oracle and Hetzner. It matched the old hostname regex, contained
    dots, and its first label was not in the blocklist, so it was stored verbatim.
    """
    with pytest.raises(ValidationError) as exc:
        UserMediaSourceCreate(
            hostname="169.254.169.254",
            provider_type="mediacms",
            username="u",
            password="p",
        )
    assert "hostname" in str(exc.value).lower()


@pytest.mark.parametrize(
    "hostname",
    [
        "169.254.169.254",  # AWS/Azure/GCP IMDS
        "169.254.170.2",  # AWS ECS task metadata
        "metadata.google.internal",  # GCP metadata by name
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC1918
        "192.168.1.1",  # RFC1918
        "172.17.0.1",  # RFC1918 — the Docker bridge gateway
        "0.0.0.0:8080",  # unspecified: routes to "this host" on most stacks
    ],
)
def test_media_source_hostname_rejects_internal_targets(hostname):
    """Every host in the audit's evidence table, all of which used to be ACCEPTED."""
    with pytest.raises(ValidationError):
        UserMediaSourceCreate(hostname=hostname, provider_type="mediacms")


def test_media_source_hostname_accepts_a_public_host(monkeypatch):
    """Control: a legitimate public host must still be storable.

    Without this, a validator that refused *everything* would satisfy every test
    above — "reject the whole feature" is not a fix.
    """
    stub_public_dns(monkeypatch)

    source = UserMediaSourceCreate(
        hostname="media.example.com",
        provider_type="mediacms",
        username="alice",
        password="secret",
    )

    assert source.hostname == "media.example.com"


def test_media_source_hostname_accepts_a_public_host_with_a_port(monkeypatch):
    """Control: ``host:port`` is a supported shape and must survive the guard.

    The stored hostname is compared against ``urlparse(url).netloc``, which carries the
    port, so refusing ports would silently break every non-443 MediaCMS install.
    """
    stub_public_dns(monkeypatch)

    source = UserMediaSourceCreate(hostname="Media.Example.com:8443")

    assert source.hostname == "media.example.com:8443"


def test_media_source_update_rejects_link_local():
    """``PUT`` shares the validator — the audit named both endpoints as reachable."""
    with pytest.raises(ValidationError):
        UserMediaSourceUpdate(hostname="169.254.169.254")


def test_media_source_update_leaves_an_omitted_hostname_alone(monkeypatch):
    """Control for the update path: a partial payload must not be forced to carry a host."""
    stub_public_dns(monkeypatch)

    update = UserMediaSourceUpdate(label="renamed")

    assert update.hostname is None
    assert update.label == "renamed"


# ---------------------------------------------------------------------------
# Request-time guard (defence in depth)
# ---------------------------------------------------------------------------

_HOSTILE_ROW = {
    "hostname": "169.254.169.254",
    "provider_type": "mediacms",
    "username": "attacker",
    "password": "hunter2",
    "verify_ssl": False,
    "label": "pre-existing hostile row",
    "user_id": 7,
}

_BENIGN_ROW = {
    "hostname": "media.example.com",
    "provider_type": "mediacms",
    "username": "alice",
    "password": "secret",
    "verify_ssl": True,
    "label": "a real install",
    "user_id": 7,
}


def _mocked_login_and_info() -> MagicMock:
    """A ``requests`` double whose login + media-info calls both succeed."""
    login_resp = MagicMock()
    login_resp.json.return_value = {"token": "auth-tok-123"}
    login_resp.raise_for_status = MagicMock()

    info_resp = MagicMock()
    info_resp.json.return_value = {"title": "A Video", "duration": 42}
    info_resp.raise_for_status = MagicMock()

    mock_requests = MagicMock()
    mock_requests.post.return_value = login_resp
    mock_requests.get.return_value = info_resp
    # The provider catches this class by name, so the double must expose the real one.
    mock_requests.exceptions.RequestException = Exception
    return mock_requests


def test_mediacms_refuses_a_private_target_at_request_time():
    """A hostile row that predates the schema fix must never be dialled.

    The schema guard cannot help here: the row is already in the database, and a
    hostname validated as public yesterday can resolve to ``169.254.169.254`` today.
    The refusal has to happen **before** any socket is opened, which is what
    ``assert_not_called`` pins — asserting only on the exception would pass against a
    provider that fetched the metadata endpoint and then complained.
    """
    provider = MediacmsProvider()
    mock_requests = _mocked_login_and_info()

    with (
        patch.object(provider, "_get_all_sources", return_value=[_HOSTILE_ROW]),
        patch("app.services.protected_media_plugins.mediacms.requests", mock_requests),
        pytest.raises(HTTPException) as exc,
    ):
        provider._login_and_get_info("https://169.254.169.254/view?m=tok", user_id=7)

    assert exc.value.status_code == 400
    mock_requests.post.assert_not_called()
    mock_requests.get.assert_not_called()

    detail = str(exc.value.detail).lower()
    for leak in ("169.254", "link-local", "metadata", "private", "loopback"):
        assert leak not in detail, f"the refusal leaked {leak!r} back to the caller"


def test_mediacms_reaches_a_public_target_at_request_time(monkeypatch):
    """Control: the guard must not refuse the legitimate path it sits on.

    Same code path, same doubles, only the stored hostname differs — so a provider that
    refused unconditionally cannot pass the test above by being broken.
    """
    stub_public_dns(monkeypatch)
    provider = MediacmsProvider()
    mock_requests = _mocked_login_and_info()

    with (
        patch.object(provider, "_get_all_sources", return_value=[_BENIGN_ROW]),
        patch("app.services.protected_media_plugins.mediacms.requests", mock_requests),
    ):
        token, base_url, info, auth_token = provider._login_and_get_info(
            "https://media.example.com/view?m=vid123", user_id=7
        )

    assert token == "vid123"
    assert base_url == "https://media.example.com"
    assert info["title"] == "A Video"
    assert auth_token == "auth-tok-123"
    mock_requests.post.assert_called_once()
    assert mock_requests.post.call_args.kwargs["url"] == "https://media.example.com/api/v1/login"


def test_mediacms_refuses_a_private_download_target(tmp_path):
    """The download leg is a third outbound call and needs its own guard.

    ``download`` builds its URL from the MediaCMS response rather than from the request,
    so a source that passed login could still hand back a path on a host that has since
    rebound. Here the whole source is hostile and the download must never start.
    """
    provider = MediacmsProvider()
    mock_requests = _mocked_login_and_info()

    with (
        patch.object(provider, "_get_all_sources", return_value=[_HOSTILE_ROW]),
        patch("app.services.protected_media_plugins.mediacms.requests", mock_requests),
        pytest.raises(HTTPException),
    ):
        provider.download("https://169.254.169.254/view?m=tok", str(tmp_path), user_id=7)

    mock_requests.get.assert_not_called()
