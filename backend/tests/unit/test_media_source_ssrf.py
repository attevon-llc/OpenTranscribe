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
from tests.helpers import stub_pinned_session
from tests.helpers import stub_public_dns

_MEDIACMS_MODULE = "app.services.protected_media_plugins.mediacms"

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


def _mocked_login_and_info_session() -> MagicMock:
    """A ``pinned_requests_session`` double whose login + media-info calls both succeed."""
    login_resp = MagicMock()
    login_resp.json.return_value = {"token": "auth-tok-123"}
    login_resp.raise_for_status = MagicMock()

    info_resp = MagicMock()
    info_resp.json.return_value = {"title": "A Video", "duration": 42}
    info_resp.raise_for_status = MagicMock()

    mock_session = MagicMock()
    mock_session.post.return_value = login_resp
    mock_session.get.return_value = info_resp
    return mock_session


def test_mediacms_refuses_a_private_target_at_request_time(monkeypatch):
    """A hostile row that predates the schema fix must never be dialled.

    The schema guard cannot help here: the row is already in the database, and a
    hostname validated as public yesterday can resolve to ``169.254.169.254`` today.
    The refusal has to happen **before** any socket is opened, which is what
    ``assert_not_called`` pins — asserting only on the exception would pass against a
    provider that fetched the metadata endpoint and then complained.
    """
    provider = MediacmsProvider()
    mock_session = _mocked_login_and_info_session()
    stub_pinned_session(monkeypatch, _MEDIACMS_MODULE, mock_session)

    with (
        patch.object(provider, "_get_all_sources", return_value=[_HOSTILE_ROW]),
        pytest.raises(HTTPException) as exc,
    ):
        provider._login_and_get_info("https://169.254.169.254/view?m=tok", user_id=7)

    assert exc.value.status_code == 400
    mock_session.post.assert_not_called()
    mock_session.get.assert_not_called()

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
    mock_session = _mocked_login_and_info_session()
    stub_pinned_session(monkeypatch, _MEDIACMS_MODULE, mock_session)

    with patch.object(provider, "_get_all_sources", return_value=[_BENIGN_ROW]):
        token, base_url, info, auth_token = provider._login_and_get_info(
            "https://media.example.com/view?m=vid123", user_id=7
        )

    assert token == "vid123"
    assert base_url == "https://media.example.com"
    assert info["title"] == "A Video"
    assert auth_token == "auth-tok-123"
    mock_session.post.assert_called_once()
    # The request is pinned: the URL dialled carries the *validated* address, not the
    # hostname (that is the whole point — see `resolve_pinned_target`), while the
    # `Host` header preserves the original name for virtual hosting.
    dialled_url = mock_session.post.call_args.args[0]
    assert dialled_url.startswith("https://")
    assert dialled_url.endswith("/api/v1/login")
    assert "media.example.com" not in dialled_url
    assert mock_session.post.call_args.kwargs["headers"]["Host"] == "media.example.com"


def test_mediacms_refuses_a_private_download_target(tmp_path, monkeypatch):
    """The download leg is a third outbound call and needs its own guard.

    ``download`` builds its URL from the MediaCMS response rather than from the request,
    so a source that passed login could still hand back a path on a host that has since
    rebound. Here the whole source is hostile and the download must never start.
    """
    provider = MediacmsProvider()
    mock_session = _mocked_login_and_info_session()
    stub_pinned_session(monkeypatch, _MEDIACMS_MODULE, mock_session)

    with (
        patch.object(provider, "_get_all_sources", return_value=[_HOSTILE_ROW]),
        pytest.raises(HTTPException),
    ):
        provider.download("https://169.254.169.254/view?m=tok", str(tmp_path), user_id=7)

    mock_session.get.assert_not_called()


def test_mediacms_media_info_redirect_is_not_followed(monkeypatch):
    """The exact exploit in finding A1: a benign login followed by a redirecting
    media-info response must not be chased to a private/link-local target.

    Before this fix, none of the three outbound calls in ``mediacms.py`` passed
    ``allow_redirects=False``, so a MediaCMS host that validated fine on the login leg
    (this test's ``_BENIGN_ROW``) could answer the media-info leg with a 302 to
    ``169.254.169.254`` and ``requests`` would follow it with no check on the redirect
    target at all. The response body — here the metadata endpoint's JSON — would then
    land in ``media_info["mediacms_raw"]`` and be returned to the caller. A test double
    cannot make a real client chase a Location header, so the falsifiable claim here is
    the one that actually stops it: the media-info request is issued with
    ``allow_redirects=False``. Watched red against ``git archive HEAD``: the call carried
    no such kwarg at all.
    """
    stub_public_dns(monkeypatch)
    provider = MediacmsProvider()
    mock_session = _mocked_login_and_info_session()
    # Simulate a compromised host: the media-info leg answers with a redirect to cloud
    # instance metadata rather than JSON. `raise_for_status()` does not raise on a 3xx.
    mock_session.get.return_value.status_code = 302
    mock_session.get.return_value.headers = {
        "Location": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
    }
    stub_pinned_session(monkeypatch, _MEDIACMS_MODULE, mock_session)

    with patch.object(provider, "_get_all_sources", return_value=[_BENIGN_ROW]):
        provider._login_and_get_info("https://media.example.com/view?m=vid123", user_id=7)

    info_call = mock_session.get.call_args
    assert info_call.kwargs.get("allow_redirects") is False
