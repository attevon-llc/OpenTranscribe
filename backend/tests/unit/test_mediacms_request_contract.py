"""MediaCMS plugin: the three outbound requests, credential precedence and the download file.

Characterization tests pinned before the rewrite of
``protected_media_plugins/mediacms.py``. Every outbound request goes through the module's
``_pin_media_url`` and ``pinned_requests_session``; most tests here replace both with
recorders so the exact URL pinned, the target dialled and the request options can be read
back. ``test_download_leg_is_pinned_again_and_refused_on_its_own`` keeps the real pin and
refuses only the download address. Tests marked "current behaviour, see defect D-n" pin a
known weakness on purpose (listed in the rewrite's defect register).
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest
import requests

from app.core.exceptions import InvalidInputError
from app.core.exceptions import UpstreamServiceError
from app.services.protected_media_plugins import mediacms
from app.services.protected_media_plugins.mediacms import MediacmsProvider
from app.utils import media_origin
from tests.helpers import stub_pinned_session
from tests.helpers import stub_public_dns

PINNED_IP = "198.51.100.7"
BASE = "https://m.example.com:8443"
URL = f"{BASE}/view?m=vid1"
_BAD_URL = "The provided URL could not be used. It must be a publicly reachable http(s) address."
_NO_CREDS = (
    "Credentials for this media source are not configured. Either provide credentials in "
    "the request or configure them in Settings > Media Sources."
)
_INFO_FAILED = "Could not fetch media information from the configured media source."
_DOWNLOAD_FAILED = "Could not download the media file from the configured media source."


@pytest.fixture(autouse=True)
def _no_env_sources(monkeypatch):
    monkeypatch.delenv("MEDIACMS_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MEDIACMS_VERIFY_SSL", raising=False)


class _Wire:
    """Every pin and every session the provider opens, and the one fake session behind them."""

    def __init__(self, pin_headers: dict[str, str] | None = None) -> None:
        self.pins: list[str] = []
        self.targets: list[Any] = []
        self.session = MagicMock()
        self.pin_headers = pin_headers or {}

    def pin(self, url: str, *, purpose: str) -> Any:
        self.pins.append(url)
        host = urlparse(url).netloc
        return SimpleNamespace(
            url=url.replace(host, PINNED_IP, 1), headers={"Host": host, **self.pin_headers}
        )

    @contextlib.contextmanager
    def open(self, target: Any):
        self.targets.append(target)
        yield self.session


def _wire(monkeypatch, **kw: Any) -> _Wire:
    wire = _Wire(**kw)
    monkeypatch.setattr(mediacms, "_pin_media_url", wire.pin)
    monkeypatch.setattr(mediacms, "pinned_requests_session", wire.open)
    return wire


def _json(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    return resp


def _stream(chunks: Any, length: str | None) -> MagicMock:
    resp = MagicMock()
    resp.headers = {} if length is None else {"Content-Length": length}
    if callable(chunks):
        resp.iter_content.side_effect = chunks
    else:
        resp.iter_content.return_value = chunks
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _url_and_options(call: Any) -> tuple[str, dict[str, Any]]:
    kwargs = dict(call.kwargs)
    return (call.args[0] if call.args else kwargs.pop("url")), kwargs


def _provider(monkeypatch, rows: list[dict]) -> MediacmsProvider:
    provider = MediacmsProvider()
    monkeypatch.setattr(provider, "_get_all_sources", lambda user_id=None: rows)
    return provider


_PORT_SOURCE = {
    "hostname": "m.example.com:8443",
    "username": "stored-user",
    "password": "stored-pass",
    "verify_ssl": False,
}

# ---------------------------------------------------------------- login and media-info legs


def test_login_and_info_legs_target_the_configured_origin_with_its_tls_setting(monkeypatch):
    provider = _provider(monkeypatch, [_PORT_SOURCE])
    wire = _wire(monkeypatch)
    wire.session.post.return_value = _json({"token": "auth-tok"})
    wire.session.get.return_value = _json({"title": "T"})

    result = provider._login_and_get_info(URL, user_id=3)

    assert result == ("vid1", BASE, {"title": "T"}, "auth-tok")
    assert wire.pins == [f"{BASE}/api/v1/login", f"{BASE}/api/v1/media/vid1"]
    assert len(wire.targets) == 2 and wire.targets[0] is not wire.targets[1]
    login_url, login_opts = _url_and_options(wire.session.post.call_args)
    assert login_url == wire.targets[0].url == f"https://{PINNED_IP}/api/v1/login"
    assert login_opts == {
        "data": {"username": "stored-user", "password": "stored-pass"},
        "headers": {"Host": "m.example.com:8443"},
        "timeout": 30,
        "verify": False,
        "allow_redirects": False,
    }
    info_url, info_opts = _url_and_options(wire.session.get.call_args)
    assert info_url == wire.targets[1].url
    assert info_opts == {
        "headers": {
            "authorization": "Token auth-tok",
            "accept": "application/json",
            "Host": "m.example.com:8443",
        },
        "timeout": 30,
        "verify": False,
        "allow_redirects": False,
    }


def test_the_pins_headers_win_over_the_providers_own(monkeypatch):
    provider = _provider(monkeypatch, [_PORT_SOURCE])
    wire = _wire(monkeypatch, pin_headers={"authorization": "pinned", "accept": "pinned"})
    wire.session.post.return_value = _json({"token": "auth-tok"})
    wire.session.get.return_value = _json({})

    provider._login_and_get_info(URL, user_id=3)

    _, info_opts = _url_and_options(wire.session.get.call_args)
    assert info_opts["headers"]["authorization"] == "pinned"
    assert info_opts["headers"]["accept"] == "pinned"


def test_the_friendly_token_goes_into_the_info_path_unencoded(monkeypatch):
    """Current behaviour, see defect D-8."""
    provider = _provider(monkeypatch, [_PORT_SOURCE])
    wire = _wire(monkeypatch)
    wire.session.post.return_value = _json({"token": "auth-tok"})
    wire.session.get.return_value = _json({})

    provider._login_and_get_info(f"{BASE}/view?m=../../x%2Fy", user_id=3)

    assert wire.pins[1] == f"{BASE}/api/v1/media/../../x/y"


@pytest.mark.parametrize(
    ("given", "stored", "sent"),
    [
        (("req-user", None), ("stored-user", "stored-pass"), ("req-user", "stored-pass")),
        ((None, "req-pass"), ("stored-user", "stored-pass"), ("stored-user", "req-pass")),
        (("", "req-pass"), ("stored-user", "stored-pass"), ("stored-user", "req-pass")),
        ((None, None), ("stored-user", "stored-pass"), ("stored-user", "stored-pass")),
    ],
)
def test_request_credentials_beat_stored_ones_field_by_field(monkeypatch, given, stored, sent):
    provider = _provider(monkeypatch, [_PORT_SOURCE])
    looked_up: list[tuple] = []

    def _stored(hostname, user_id=None, scheme=None):
        looked_up.append((hostname, user_id, scheme))
        return stored

    monkeypatch.setattr(provider, "_get_stored_credentials", _stored)
    wire = _wire(monkeypatch)
    wire.session.post.return_value = _json({"token": "auth-tok"})
    wire.session.get.return_value = _json({})

    provider._login_and_get_info(URL, username=given[0], password=given[1], user_id=9)

    _, login_opts = _url_and_options(wire.session.post.call_args)
    assert (login_opts["data"]["username"], login_opts["data"]["password"]) == sent
    assert looked_up == [("m.example.com:8443", 9, "https")]


def test_complete_request_credentials_skip_the_stored_lookup(monkeypatch):
    provider = _provider(monkeypatch, [_PORT_SOURCE])
    looked_up: list[str] = []

    def _stored(hostname, user_id=None, scheme=None):
        looked_up.append(hostname)
        return None, None

    monkeypatch.setattr(provider, "_get_stored_credentials", _stored)
    wire = _wire(monkeypatch)
    wire.session.post.return_value = _json({"token": "auth-tok"})
    wire.session.get.return_value = _json({})

    provider._login_and_get_info(URL, username="u", password="p", user_id=9)

    _, login_opts = _url_and_options(wire.session.post.call_args)
    assert login_opts["data"] == {"username": "u", "password": "p"}
    assert looked_up == []


def test_missing_credentials_are_refused_before_anything_is_pinned(monkeypatch):
    provider = _provider(monkeypatch, [])
    monkeypatch.setattr(provider, "_get_stored_credentials", lambda *a, **k: ("stored-user", None))
    wire = _wire(monkeypatch)

    with pytest.raises(InvalidInputError) as exc:
        # The host is not configured either; the credential check comes first.
        provider._login_and_get_info("https://elsewhere.example.com/view?m=x", username="u")

    assert (exc.value.status_code, exc.value.detail) == (400, _NO_CREDS)
    assert wire.pins == []


def test_an_empty_auth_token_is_a_502_and_the_info_leg_is_never_requested(monkeypatch):
    provider = _provider(monkeypatch, [_PORT_SOURCE])
    wire = _wire(monkeypatch)
    wire.session.post.return_value = _json({"token": ""})

    with pytest.raises(UpstreamServiceError) as exc:
        provider._login_and_get_info(URL, user_id=3)

    assert (exc.value.status_code, exc.value.detail) == (
        502,
        "MediaCMS login did not return an auth token",
    )
    wire.session.get.assert_not_called()
    assert wire.pins == [f"{BASE}/api/v1/login"]


def test_a_rejected_login_is_an_opaque_502(monkeypatch):
    provider = _provider(monkeypatch, [_PORT_SOURCE])
    wire = _wire(monkeypatch)
    login = _json({})
    login.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "401 Client Error for url: https://10.1.2.3:8443/api/v1/login"
    )
    wire.session.post.return_value = login

    with pytest.raises(UpstreamServiceError) as exc:
        provider._login_and_get_info(URL, user_id=3)

    assert (exc.value.status_code, exc.value.detail) == (502, _INFO_FAILED)
    assert isinstance(exc.value.__cause__, requests.exceptions.HTTPError)
    assert "10.1.2.3" not in str(exc.value)
    wire.session.get.assert_not_called()


def test_a_failed_info_response_is_an_opaque_502(monkeypatch):
    provider = _provider(monkeypatch, [_PORT_SOURCE])
    wire = _wire(monkeypatch)
    wire.session.post.return_value = _json({"token": "auth-tok"})
    info = _json({})
    info.raise_for_status.side_effect = requests.exceptions.HTTPError("404 for 10.1.2.3")
    wire.session.get.return_value = info

    with pytest.raises(UpstreamServiceError) as exc:
        provider._login_and_get_info(URL, user_id=3)

    assert (exc.value.status_code, exc.value.detail) == (502, _INFO_FAILED)
    assert "10.1.2.3" not in str(exc.value)


# ---------------------------------------------------------------- download leg

_INFO = {"title": "Talk", "original_media_url": "/media/original/v.mp4"}


def _download_provider(monkeypatch, info: dict) -> tuple[MediacmsProvider, list, list]:
    """A provider whose login is already done; records the login and TLS lookups."""
    provider = MediacmsProvider()
    logins: list[tuple] = []
    tls: list[tuple] = []

    def _login(url, username=None, password=None, user_id=None):
        logins.append((url, username, password, user_id))
        return "vid1", BASE, info, "tok"

    def _verify(hostname, user_id=None, scheme=None):
        tls.append((hostname, user_id, scheme))
        return False

    monkeypatch.setattr(provider, "_login_and_get_info", _login)
    monkeypatch.setattr(provider, "_get_verify_ssl_for_host", _verify)
    return provider, logins, tls


def test_download_request_shape(monkeypatch, tmp_path):
    provider, logins, tls = _download_provider(monkeypatch, _INFO)
    wire = _wire(monkeypatch)
    resp = _stream([b"abc"], "3")
    wire.session.get.return_value = resp

    result = provider.download(URL, str(tmp_path), username="u", password="p", user_id=5)

    assert logins == [(URL, "u", "p", 5)]
    assert tls == [("m.example.com:8443", 5, "https")]
    assert wire.pins == [f"{BASE}/media/original/v.mp4"]
    url, options = _url_and_options(wire.session.get.call_args)
    assert url == wire.targets[0].url
    assert options == {
        "stream": True,
        "timeout": 300,
        "verify": False,
        "headers": {"authorization": "Token tok", "Host": "m.example.com:8443"},
        "allow_redirects": False,
    }
    resp.raise_for_status.assert_called_once_with()
    assert resp.iter_content.call_args.kwargs == {"chunk_size": 1024 * 1024}
    assert result == {
        "file_path": str(tmp_path / "Talk.mp4"),
        "filename": "Talk.mp4",
        "info": provider._build_media_info("vid1", BASE, _INFO, URL),
    }
    assert (tmp_path / "Talk.mp4").read_bytes() == b"abc"


@pytest.mark.parametrize(
    ("info", "filename"),
    [
        ({"title": "a/b:c?d\\e"}, "a_b_c_d_e.mp4"),
        # Current behaviour, see defect D-4: any dot counts as an extension.
        ({"title": "Q3 v1.2 review"}, "Q3 v1.2 review"),
        ({"title": "x" * 300}, "x" * 200 + ".mp4"),
        ({"title": "", "name": "Named"}, "Named.mp4"),
        ({}, "vid1.mp4"),
        ({"title": "café-talk_1"}, "café-talk_1.mp4"),
        ({"title": 12345}, "12345.mp4"),
    ],
)
def test_download_filename_rule(monkeypatch, tmp_path, info, filename):
    provider, _, _ = _download_provider(
        monkeypatch, {**info, "original_media_url": "/media/original/v.mp4"}
    )
    wire = _wire(monkeypatch)
    wire.session.get.return_value = _stream([b"abc"], None)

    result = provider.download(URL, str(tmp_path), username="u", password="p", user_id=5)

    assert result["filename"] == filename
    assert Path(result["file_path"]) == tmp_path / filename
    assert (tmp_path / filename).read_bytes() == b"abc"


@pytest.mark.parametrize(
    ("chunks", "length", "progress"),
    [
        ([b"a", b"", b"b", b"cd"], "4", [20, 30, 40, 60]),
        ([b"ab", b"ab"], "2", [20, 60, 60]),
        ([b"abc"], "6", [20, 40]),
        ([b"ab", b"cd"], None, [20]),
        ([b"ab", b"cd"], "0", [20]),
    ],
)
def test_download_progress(monkeypatch, tmp_path, chunks, length, progress):
    provider, _, _ = _download_provider(monkeypatch, _INFO)
    wire = _wire(monkeypatch)
    wire.session.get.return_value = _stream(chunks, length)
    seen: list[int] = []

    provider.download(URL, str(tmp_path), progress_callback=lambda p, _m: seen.append(p), user_id=5)

    assert seen == progress
    assert (tmp_path / "Talk.mp4").read_bytes() == b"".join(chunks)


def test_download_without_a_progress_callback(monkeypatch, tmp_path):
    provider, _, _ = _download_provider(monkeypatch, _INFO)
    wire = _wire(monkeypatch)
    wire.session.get.return_value = _stream([b"ab"], "2")

    result = provider.download(URL, str(tmp_path), user_id=5)

    assert Path(result["file_path"]).read_bytes() == b"ab"


def test_download_with_no_media_url_is_refused_before_any_pin(monkeypatch, tmp_path):
    provider, _, _ = _download_provider(monkeypatch, {"title": "t", "original_media_url": ""})
    wire = _wire(monkeypatch)

    with pytest.raises(UpstreamServiceError) as exc:
        provider.download(URL, str(tmp_path), user_id=5)

    assert (exc.value.status_code, exc.value.detail) == (
        502,
        "MediaCMS media info is missing 'original_media_url'",
    )
    assert wire.pins == []


def _broken_stream(exc: BaseException):
    def _chunks(chunk_size):
        yield b"partial"
        raise exc

    return _chunks


def test_a_download_interrupted_by_the_network_removes_the_partial_file(monkeypatch, tmp_path):
    provider, _, _ = _download_provider(monkeypatch, _INFO)
    wire = _wire(monkeypatch)
    cause = requests.exceptions.ChunkedEncodingError("reset by 10.1.2.3")
    wire.session.get.return_value = _stream(_broken_stream(cause), "100")

    with pytest.raises(UpstreamServiceError) as exc:
        provider.download(URL, str(tmp_path), user_id=5)

    assert (exc.value.status_code, exc.value.detail) == (502, _DOWNLOAD_FAILED)
    assert exc.value.__cause__ is cause
    assert "10.1.2.3" not in str(exc.value)
    assert list(tmp_path.iterdir()) == []


def test_a_rejected_download_is_an_opaque_502_with_no_file(monkeypatch, tmp_path):
    provider, _, _ = _download_provider(monkeypatch, _INFO)
    wire = _wire(monkeypatch)
    resp = _stream([b"never"], "5")
    resp.raise_for_status.side_effect = requests.exceptions.HTTPError("403")
    wire.session.get.return_value = resp

    with pytest.raises(UpstreamServiceError) as exc:
        provider.download(URL, str(tmp_path), user_id=5)

    assert (exc.value.status_code, exc.value.detail) == (502, _DOWNLOAD_FAILED)
    assert list(tmp_path.iterdir()) == []


def test_a_non_numeric_content_length_escapes_unwrapped(monkeypatch, tmp_path):
    """Current behaviour, see defect D-5."""
    provider, _, _ = _download_provider(monkeypatch, _INFO)
    wire = _wire(monkeypatch)
    wire.session.get.return_value = _stream([b"ab"], "lots")

    with pytest.raises(ValueError, match="lots"):
        provider.download(URL, str(tmp_path), user_id=5)

    assert list(tmp_path.iterdir()) == []


def test_a_local_write_failure_escapes_and_leaves_the_partial_file(monkeypatch, tmp_path):
    """Current behaviour, see defect D-5."""
    provider, _, _ = _download_provider(monkeypatch, _INFO)
    wire = _wire(monkeypatch)
    wire.session.get.return_value = _stream(_broken_stream(OSError("disk full")), "100")

    with pytest.raises(OSError, match="disk full"):
        provider.download(URL, str(tmp_path), user_id=5)

    assert (tmp_path / "Talk.mp4").read_bytes() == b"partial"


def test_download_leg_is_pinned_again_and_refused_on_its_own(monkeypatch, tmp_path, caplog):
    """The download address is validated afresh; a refusal there stops before the GET."""
    stub_public_dns(monkeypatch)
    provider = _provider(monkeypatch, [_PORT_SOURCE])
    session = MagicMock()
    session.post.return_value = _json({"token": "auth-tok"})
    session.get.return_value = _json(_INFO)
    stub_pinned_session(monkeypatch, "app.services.protected_media_plugins.mediacms", session)
    real_resolve = media_origin.resolve_pinned_target
    checked: list[str] = []

    def _refuse_the_download(url):
        checked.append(url)
        if "/media/original/" in url:
            return None, "resolves to a private address"
        return real_resolve(url)

    monkeypatch.setattr(media_origin, "resolve_pinned_target", _refuse_the_download)

    with caplog.at_level(logging.WARNING), pytest.raises(InvalidInputError) as exc:
        provider.download(URL, str(tmp_path), user_id=5)

    assert (exc.value.status_code, exc.value.detail) == (400, _BAD_URL)
    assert checked == [
        f"{BASE}/api/v1/login",
        f"{BASE}/api/v1/media/vid1",
        f"{BASE}/media/original/v.mp4",
    ]
    assert session.get.call_count == 1
    assert list(tmp_path.iterdir()) == []
    assert "resolves to a private address" in caplog.text


def test_credentials_never_reach_the_logs(monkeypatch, tmp_path, caplog):
    provider = _provider(monkeypatch, [_PORT_SOURCE])
    wire = _wire(monkeypatch)
    wire.session.post.return_value = _json({"token": "auth-tok"})
    wire.session.get.side_effect = [
        _json(_INFO),
        requests.exceptions.ConnectionError("refused"),
    ]

    with caplog.at_level(logging.DEBUG), pytest.raises(UpstreamServiceError):
        provider.download(URL, str(tmp_path), username="alice-login", password="pw-s3cret")

    assert any(r.levelno >= logging.ERROR for r in caplog.records), "the failure was logged"
    assert "alice-login" not in caplog.text
    assert "pw-s3cret" not in caplog.text
