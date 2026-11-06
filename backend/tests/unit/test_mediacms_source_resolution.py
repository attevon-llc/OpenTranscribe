"""MediaCMS plugin: which URLs it claims, which sources it reads, and what it exposes.

Characterization tests pinned before the rewrite of
``protected_media_plugins/mediacms.py``: they describe the behaviour the rewrite must keep,
through the provider's own methods, with the configured sources supplied as plain rows.
Tests marked "current behaviour, see defect D-n" pin a known weakness on purpose; they are
listed in the rewrite's defect register and change only when the owner decides the fix.
"""

from __future__ import annotations

import inspect

import pytest

from app.core.exceptions import InvalidInputError
from app.services import protected_media_providers
from app.services.protected_media_plugins import mediacms
from app.services.protected_media_plugins.mediacms import MediacmsProvider

_NOT_CONFIGURED = "URL hostname is not in the configured media sources"
_NO_TOKEN = "Missing media token (m query param or /api/v1/media/<token>)"


@pytest.fixture(autouse=True)
def _no_env_sources(monkeypatch):
    monkeypatch.delenv("MEDIACMS_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MEDIACMS_VERIFY_SSL", raising=False)


def _provider_with(monkeypatch, rows: list[dict]) -> MediacmsProvider:
    """A provider whose combined source list (per-user, then system) is ``rows``."""
    provider = MediacmsProvider()
    monkeypatch.setattr(provider, "_get_all_sources", lambda user_id=None: rows)
    return provider


# ---------------------------------------------------------------- URL recognition


@pytest.mark.parametrize(
    ("url", "claimed"),
    [
        ("https://m.example.com/view?m=x", True),
        # A source configured without a scheme is https only: its stored credential is
        # never offered to a plain-http URL on the same host.
        ("http://m.example.com/view?m=x", False),
        ("http://m.example.com:443/view?m=x", False),
        ("https://M.Example.com:443/view?m=x", True),
        ("https://m.example.com/api/v1/media/tok", True),
        # Current behaviour, see defect D-7: the token segment is not required here.
        ("https://m.example.com/api/v1/media", True),
        ("https://m.example.com:8443/view?m=x", False),
        ("https://u@m.example.com/view?m=x", False),
        ("https://m.example.com/api/v2/media/tok", False),
        ("https://m.example.com/mediacms/api/v1/media/tok", False),
        ("https://m.example.com/view?m=", False),
        ("https://m.example.com/view?n=x", False),
        ("ftp://m.example.com/view?m=x", False),
        ("https://other.example.com/view?m=x", False),
    ],
)
def test_which_urls_the_provider_claims(monkeypatch, url, claimed):
    provider = _provider_with(monkeypatch, [{"hostname": "m.example.com"}])
    assert provider.can_handle(url, user_id=1) is claimed


def test_a_failing_host_lookup_means_not_claimed(monkeypatch):
    provider = MediacmsProvider()

    def _boom(user_id=None):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(provider, "_get_allowed_hosts", _boom)
    assert provider.can_handle("https://m.example.com/view?m=x", user_id=1) is False


# ---------------------------------------------------------------- token and base URL


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://m.example.com/view?m=a&m=b", ("a", "https://m.example.com")),
        ("https://m.example.com/api/v1/media/tok/extra?x=1#f", ("tok", "https://m.example.com")),
        ("https://m.example.com/api/v1/media/path-tok?m=q", ("q", "https://m.example.com")),
        ("https://m.example.com:8443/view?m=t#frag", ("t", "https://m.example.com:8443")),
        ("https://M.Example.com/view?m=t", ("t", "https://m.example.com")),
        ("http://P.example.com:8080/view?m=t", ("t", "http://p.example.com:8080")),
        ("https://m.example.com:443/view?m=t", ("t", "https://m.example.com")),
        # Current behaviour, see defect D-13: a path prefix is dropped from the base URL.
        ("https://m.example.com/mediacms/view?m=t", ("t", "https://m.example.com")),
    ],
)
def test_token_and_base_url(monkeypatch, url, expected):
    provider = _provider_with(
        monkeypatch,
        [
            {"hostname": "m.example.com"},
            {"hostname": "m.example.com:8443"},
            {"hostname": "http://p.example.com:8080"},
        ],
    )
    assert provider._get_token_and_base_url(url, user_id=1) == expected


@pytest.mark.parametrize(
    ("url", "detail"),
    [
        ("https://u@m.example.com/view?m=t", _NOT_CONFIGURED),
        ("https://m.example.com:9443/view?m=t", _NOT_CONFIGURED),
        ("http://m.example.com/view?m=t", _NOT_CONFIGURED),
        ("http://m.example.com:443/view?m=t", _NOT_CONFIGURED),
        ("https://m.example.com/api/v1/media", _NO_TOKEN),
        ("https://m.example.com/view?m=", _NO_TOKEN),
        ("https://m.example.com/mediacms/api/v1/media/tok", _NO_TOKEN),
    ],
)
def test_token_and_base_url_refusals(monkeypatch, url, detail):
    provider = _provider_with(monkeypatch, [{"hostname": "m.example.com"}])
    with pytest.raises(InvalidInputError) as exc:
        provider._get_token_and_base_url(url, user_id=1)
    assert exc.value.status_code == 400
    assert exc.value.detail == detail


# ---------------------------------------------------------------- sources and env fallback


@pytest.mark.parametrize(
    ("value", "verify"),
    [
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("No", False),
        ("true", True),
        ("1", True),
        ("off", True),
        ("", True),
        (None, True),
    ],
)
def test_env_tls_default_for_a_host_with_no_source(monkeypatch, value, verify):
    if value is not None:
        monkeypatch.setenv("MEDIACMS_VERIFY_SSL", value)
    provider = _provider_with(monkeypatch, [])
    assert provider._get_verify_ssl_for_host("m.example.com", user_id=1, scheme="https") is verify


def test_the_first_matching_source_decides_tls_verification(monkeypatch):
    monkeypatch.setenv("MEDIACMS_VERIFY_SSL", "false")
    provider = _provider_with(
        monkeypatch,
        [
            {"hostname": "other.example.com", "verify_ssl": False},
            {"hostname": "M.example.com", "verify_ssl": 1},
            {"hostname": "m.example.com", "verify_ssl": False},
            {"hostname": "nokey.example.com"},
            {"hostname": "zero.example.com", "verify_ssl": 0},
        ],
    )
    assert provider._get_verify_ssl_for_host("m.example.com", user_id=1, scheme="https") is True
    assert provider._get_verify_ssl_for_host("nokey.example.com", user_id=1, scheme="https") is True
    assert provider._get_verify_ssl_for_host("zero.example.com", user_id=1, scheme="https") is False
    assert (
        provider._get_verify_ssl_for_host("unknown.example.com", user_id=1, scheme="https") is False
    )
    # The https source's setting never applies to the same host over http.
    assert provider._get_verify_ssl_for_host("zero.example.com", user_id=1, scheme="http") is False
    assert provider._get_verify_ssl_for_host("m.example.com", user_id=1, scheme="http") is False


def test_stored_credentials_come_from_the_first_complete_matching_source(monkeypatch):
    monkeypatch.setenv("MEDIACMS_ALLOWED_HOSTS", "x.example.com")
    provider = _provider_with(
        monkeypatch,
        [
            {"hostname": "m.example.com", "username": "only-user", "password": ""},
            {"hostname": "m.example.com", "username": None, "password": "only-pass"},
            {"hostname": "M.EXAMPLE.COM:443", "username": "first", "password": "one"},
            {"hostname": "m.example.com", "username": "second", "password": "two"},
            {"hostname": "http://m.example.com", "username": "plain", "password": "text"},
        ],
    )
    assert provider._get_stored_credentials("m.example.com", user_id=1, scheme="https") == (
        "first",
        "one",
    )
    assert provider._get_stored_credentials("m.example.com", user_id=1, scheme="http") == (
        "plain",
        "text",
    )
    assert provider._get_stored_credentials("m.example.com:80", user_id=1, scheme="http") == (
        "plain",
        "text",
    )
    assert provider._get_stored_credentials("x.example.com", user_id=1, scheme="https") == (
        None,
        None,
    )


def test_allowed_hosts_are_stripped_source_hosts_plus_the_env_list(monkeypatch):
    monkeypatch.setenv("MEDIACMS_ALLOWED_HOSTS", " e1.example.com , ,e2.example.com,")
    provider = _provider_with(
        monkeypatch,
        [{"hostname": " s1.example.com "}, {"hostname": ""}, {"hostname": "   "}, {}],
    )
    assert provider._get_allowed_hosts(1) == {"s1.example.com", "e1.example.com", "e2.example.com"}


def test_all_sources_are_the_callers_sources_then_the_system_ones(monkeypatch):
    provider = MediacmsProvider()
    seen: list[int | None] = []

    def _user_sources(user_id=None):
        seen.append(user_id)
        return [{"hostname": "own.example.com"}]

    monkeypatch.setattr(provider, "_get_user_media_sources", _user_sources)
    monkeypatch.setattr(provider, "_get_db_sources", lambda: [{"hostname": "sys.example.com"}])
    assert provider._get_all_sources(7) == [
        {"hostname": "own.example.com"},
        {"hostname": "sys.example.com"},
    ]
    assert seen == [7]


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_system_sources_keep_only_mediacms_rows_and_close_their_session(monkeypatch):
    session = _FakeSession()
    rows = [
        {"hostname": "a.example.com", "provider_type": "mediacms"},
        {"hostname": "b.example.com", "provider_type": "other"},
        {"hostname": "c.example.com"},
    ]
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: session)
    monkeypatch.setattr(
        "app.services.system_settings_service.get_system_media_sources",
        lambda db: rows if db is session else [],
    )
    assert MediacmsProvider()._get_db_sources() == [rows[0]]
    assert session.closed is True


def test_system_sources_read_failure_is_an_empty_list(monkeypatch):
    session = _FakeSession()

    def _broken(db):
        raise RuntimeError("settings table missing")

    monkeypatch.setattr("app.db.base.SessionLocal", lambda: session)
    monkeypatch.setattr("app.services.system_settings_service.get_system_media_sources", _broken)
    assert MediacmsProvider()._get_db_sources() == []
    assert session.closed is True


def test_per_user_sources_read_failure_is_an_empty_list(monkeypatch):
    def _no_database():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.db.base.SessionLocal", _no_database)
    assert MediacmsProvider()._get_user_media_sources(3) == []


# ---------------------------------------------------------------- public auth config

_FIELDS = [
    {"name": "media_username", "label": "Media username", "type": "text"},
    {"name": "media_password", "label": "Media password", "type": "password"},
]


def test_public_auth_config_shape_and_no_secret(monkeypatch):
    monkeypatch.setenv("MEDIACMS_ALLOWED_HOSTS", "e.example.com")
    provider = _provider_with(
        monkeypatch,
        [
            # Current behaviour, see defect D-17: the flag list keeps the stored spelling.
            {"hostname": " m.example.com ", "username": "u-secret", "password": "p-secret"},
            {"hostname": "n.example.com", "username": "u-secret", "password": ""},
            {"hostname": "", "username": "u-secret", "password": "p-secret"},
        ],
    )
    config = provider.get_public_auth_config(user_id=1)
    assert config == {
        "hosts": ["e.example.com", "m.example.com", "n.example.com"],
        "hosts_with_stored_credentials": [" m.example.com "],
        "auth_type": "user_password",
        "fields": _FIELDS,
    }
    assert "secret" not in repr(config)


def test_public_auth_config_with_env_hosts_only(monkeypatch):
    monkeypatch.setenv("MEDIACMS_ALLOWED_HOSTS", "b.example.com,a.example.com")
    provider = _provider_with(monkeypatch, [])
    assert provider.get_public_auth_config(user_id=None) == {
        "hosts": ["a.example.com", "b.example.com"],
        "hosts_with_stored_credentials": [],
        "auth_type": "user_password",
        "fields": _FIELDS,
    }


# ---------------------------------------------------------------- media-info mapping


def test_media_info_mapping_is_exact():
    info = {
        "name": "Named",
        "user": "bob",
        "thumbnail_url": "media/t.jpg",
        "original_media_url": "/media/original/v.mp4",
        "extra": 1,
    }
    url = "https://m.example.com/view?m=tok"
    assert MediacmsProvider()._build_media_info("tok", "https://m.example.com", info, url) == {
        "id": "tok",
        "title": "Named",
        "description": None,
        "uploader": "bob",
        "duration": None,
        "extractor": "mediacms",
        "thumbnail": "https://m.example.com/media/t.jpg",
        "original_media_url": "/media/original/v.mp4",
        "source": "mediacms",
        "original_url": url,
        "mediacms_raw": info,
        "mediacms_base_url": "https://m.example.com",
    }


@pytest.mark.parametrize(
    ("raw", "thumbnail"),
    [
        ("", None),
        ("//cdn.example.com/t.jpg", "https://cdn.example.com/t.jpg"),
        ("http://cdn.example.com/t.jpg", "http://cdn.example.com/t.jpg"),
        ("/thumbs/t.jpg", "https://m.example.com:8443/thumbs/t.jpg"),
    ],
)
def test_thumbnail_resolution(raw, thumbnail):
    info = MediacmsProvider()._build_media_info(
        "tok", "https://m.example.com:8443", {"thumbnail_url": raw}, "u"
    )
    assert info["thumbnail"] == thumbnail


def test_title_and_uploader_precedence():
    info = MediacmsProvider()._build_media_info(
        "tok", "https://m.example.com", {"title": "T", "name": "N", "owner": "o", "user": "u"}, "u"
    )
    assert (info["title"], info["uploader"]) == ("T", "o")


# ---------------------------------------------------------------- plugin registration


def test_the_real_plugin_package_registers_the_mediacms_provider():
    assert isinstance(mediacms.provider, MediacmsProvider)
    loaded = protected_media_providers._load_providers()
    assert [p for p in loaded if isinstance(p, MediacmsProvider)] == [mediacms.provider]
    assert mediacms.provider in protected_media_providers.PROTECTED_MEDIA_PROVIDERS


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("can_handle", ["url", "user_id"]),
        ("extract_info", ["url", "username", "password", "user_id"]),
        (
            "download",
            ["url", "output_path", "progress_callback", "username", "password", "user_id"],
        ),
        ("get_public_auth_config", ["user_id"]),
    ],
)
def test_the_plugin_interface_parameter_names(method, params):
    assert list(inspect.signature(getattr(mediacms.provider, method)).parameters) == params
