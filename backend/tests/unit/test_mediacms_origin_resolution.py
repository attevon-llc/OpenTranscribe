"""MediaCMS download URL construction keeps the configured origin.

The path the MediaCMS server returns is resolved as a URL reference against the configured
base; any result with a different scheme, host or port is rejected so the user's token is
only ever sent to the configured origin.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.services.protected_media_plugins import mediacms
from app.services.protected_media_plugins.mediacms import MediacmsProvider
from app.services.protected_media_plugins.mediacms import resolve_same_origin_url

BASE = "https://m.example.com"


@pytest.mark.parametrize(
    "value",
    [
        "@evil.example",
        ".evil.example",
        ":80",
        "/media/original/video.mp4",
        "media/original/video.mp4",
        "https://m.example.com/media/original/video.mp4",
    ],
)
def test_values_that_stay_on_the_origin_resolve_under_it(value):
    resolved = resolve_same_origin_url(BASE, value)
    assert resolved is not None and resolved.startswith("https://m.example.com/")


@pytest.mark.parametrize(
    "value",
    [
        "//evil.example/x",
        "http://m.example.com/x",
        "https://evil.example/x",
        "https://m.example.com:8443/x",
        "https://m.example.com@evil.example/x",
        "https://user:pw@m.example.com/x",
        "\\\\evil.example\\x",
        "/ok\r\nHost: evil",
        "",
    ],
)
def test_values_that_change_scheme_host_or_port_are_rejected(value):
    assert resolve_same_origin_url(BASE, value) is None


def test_a_configured_port_is_part_of_the_origin():
    base = "https://m.example.com:8443"
    assert resolve_same_origin_url(base, "/a") == "https://m.example.com:8443/a"
    assert resolve_same_origin_url(base, "https://m.example.com/a") is None
    assert resolve_same_origin_url(base, "//m.example.com:9/a") is None


@pytest.mark.parametrize(
    ("returned", "ok"),
    [
        ("@evil.example", True),
        (".evil.example", True),
        (":80", True),
        ("//evil.example/x", False),
        ("http://evil.example/x", False),
        ("/media/original/v.mp4", True),
    ],
)
def test_download_only_requests_the_configured_origin(returned, ok, tmp_path, monkeypatch):
    provider = MediacmsProvider()
    info = {"title": "t", "original_media_url": returned}
    requested: list[str] = []

    def _pin(url, purpose):
        requested.append(url)
        raise HTTPException(status_code=599, detail="stop before any network")

    monkeypatch.setattr(mediacms, "_pin_media_url", _pin)
    with (
        patch.object(
            provider,
            "_login_and_get_info",
            return_value=("vid1", BASE, info, "tok"),
        ),
        patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
        pytest.raises(HTTPException) as exc,
    ):
        provider.download("https://m.example.com/view?m=vid1", str(tmp_path), user_id=1)
    if ok:
        assert exc.value.status_code == 599
        assert requested and requested[0].startswith("https://m.example.com/")
    else:
        assert exc.value.status_code == 502 and "invalid media URL" in str(exc.value.detail)
        assert requested == [], "no request may be built for a foreign origin"


def test_download_url_is_not_built_by_concatenation():
    text = Path(mediacms.__file__).read_text(encoding="utf-8")
    assert 'f"{base_url}{original_media_url}"' not in text
