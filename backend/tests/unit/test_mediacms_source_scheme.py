"""MediaCMS sources carry a scheme, and credentials only travel over that scheme.

A source stored without a scheme means ``https``. Plain ``http`` is used only when the source
was configured as ``http://...``, and then with a warning in the log. The sign-in request is
addressed to the configured origin, never to whatever scheme or spelling the submitted URL used.
"""

from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest
from pydantic import ValidationError

from app.core.exceptions import InvalidInputError
from app.schemas.admin import MediaSource
from app.schemas.admin import MediaSourceCreate
from app.schemas.media_source import UserMediaSourceCreate
from app.services.protected_media_plugins import mediacms
from app.services.protected_media_plugins.mediacms import MediacmsProvider
from tests.helpers import stub_public_dns


@pytest.fixture(autouse=True)
def _no_env_sources(monkeypatch):
    monkeypatch.delenv("MEDIACMS_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MEDIACMS_VERIFY_SSL", raising=False)


class _Wire:
    """Records every URL the provider pins and answers with one fake session."""

    def __init__(self) -> None:
        self.pins: list[str] = []
        self.session = MagicMock()
        self.session.post.return_value.json.return_value = {"token": "auth-tok"}
        self.session.get.return_value.json.return_value = {"title": "T"}

    def pin(self, url: str, *, purpose: str) -> Any:
        self.pins.append(url)
        return SimpleNamespace(url=url, headers={"Host": urlparse(url).netloc})

    @contextlib.contextmanager
    def open(self, target: Any):
        yield self.session


def _setup(monkeypatch, hostname: str) -> tuple[MediacmsProvider, _Wire]:
    provider = MediacmsProvider()
    row = {"hostname": hostname, "username": "stored-user", "password": "stored-pass"}
    monkeypatch.setattr(provider, "_get_all_sources", lambda user_id=None: [row])
    wire = _Wire()
    monkeypatch.setattr(mediacms, "_pin_media_url", wire.pin)
    monkeypatch.setattr(mediacms, "pinned_requests_session", wire.open)
    return provider, wire


# ---------------------------------------------------------------- the sign-in URL


@pytest.mark.parametrize(
    "submitted", ["http://m.example.com/view?m=v", "http://m.example.com:443/view?m=v"]
)
def test_a_scheme_less_source_never_signs_in_over_http(monkeypatch, submitted):
    provider, wire = _setup(monkeypatch, "m.example.com")

    with pytest.raises(InvalidInputError) as exc:
        provider.extract_info(submitted, user_id=1)

    # Refused before anything is sent: the https source's credential is not found for an
    # http URL, and the host is not a configured source for that scheme either.
    assert exc.value.status_code == 400
    assert provider.can_handle(submitted, user_id=1) is False
    assert wire.pins == []
    wire.session.post.assert_not_called()


def test_the_sign_in_url_comes_from_the_configured_source_not_the_submitted_url(monkeypatch):
    provider, wire = _setup(monkeypatch, "m.example.com")

    provider.extract_info("https://M.Example.COM:443/view?m=v", user_id=1)

    assert wire.pins == [
        "https://m.example.com/api/v1/login",
        "https://m.example.com/api/v1/media/v",
    ]
    assert wire.session.post.call_args.kwargs["data"] == {
        "username": "stored-user",
        "password": "stored-pass",
    }


def test_an_https_server(monkeypatch, caplog):
    provider, wire = _setup(monkeypatch, "https://m.example.com:8443")

    with caplog.at_level(logging.WARNING, logger=mediacms.logger.name):
        provider.extract_info("https://m.example.com:8443/view?m=v", user_id=1)

    assert wire.pins[0] == "https://m.example.com:8443/api/v1/login"
    assert "plain http" not in caplog.text


def test_an_http_server_configured_explicitly_works_and_is_logged(monkeypatch, caplog):
    provider, wire = _setup(monkeypatch, "http://m.example.com:8080")

    with caplog.at_level(logging.WARNING, logger=mediacms.logger.name):
        provider.extract_info("http://m.example.com:8080/view?m=v", user_id=1)

    assert wire.pins == [
        "http://m.example.com:8080/api/v1/login",
        "http://m.example.com:8080/api/v1/media/v",
    ]
    assert "m.example.com:8080 is configured for plain http" in caplog.text


def test_an_http_source_does_not_match_https_urls_either(monkeypatch):
    provider, wire = _setup(monkeypatch, "http://m.example.com")

    assert provider.can_handle("https://m.example.com/view?m=v", user_id=1) is False
    with pytest.raises(InvalidInputError):
        provider.extract_info("https://m.example.com/view?m=v", user_id=1)
    wire.session.post.assert_not_called()


# ---------------------------------------------------------------- what is stored


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("media.example.com", "https://media.example.com"),
        ("HTTPS://Media.Example.com:8443/", "https://media.example.com:8443"),
        ("http://media.example.com", "http://media.example.com"),
        ("http://media.example.com:8080", "http://media.example.com:8080"),
    ],
)
def test_the_stored_address_names_its_scheme(monkeypatch, given, stored):
    stub_public_dns(monkeypatch)

    assert UserMediaSourceCreate(hostname=given).hostname == stored
    assert MediaSourceCreate(hostname=given).hostname == stored


def test_saving_an_http_source_is_logged(monkeypatch, caplog):
    stub_public_dns(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.schemas.media_source"):
        UserMediaSourceCreate(hostname="http://media.example.com")

    assert "configured for plain http" in caplog.text


@pytest.mark.parametrize(
    "given",
    [
        "ftp://media.example.com",
        "https://media.example.com/mediacms",
        "https://",
        "http://169.254.169.254",
        "http://10.0.0.5:8080",
    ],
)
def test_unusable_addresses_are_refused(monkeypatch, given):
    stub_public_dns(monkeypatch)

    with pytest.raises(ValidationError):
        UserMediaSourceCreate(hostname=given)


def test_a_legacy_system_source_reads_back_as_https():
    legacy = MediaSource(id="a1", hostname="media.example.com", provider_type="mediacms")

    assert legacy.hostname == "https://media.example.com"
