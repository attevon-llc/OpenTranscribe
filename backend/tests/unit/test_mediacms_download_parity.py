"""MediaCMS download progress and thumbnail parsing on hostile server values.

A negative Content-Length must not produce progress below the download floor, and a
non-string thumbnail_url in media metadata must not fail the import.
"""

import contextlib
import types

import pytest

from app.services.protected_media_plugins import mediacms


class _StreamedResponse:
    def __init__(self, chunks, content_length):
        self.headers = {"Content-Length": content_length}
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield from self._chunks


def _provider_serving(monkeypatch, response):
    provider = mediacms.MediacmsProvider()
    monkeypatch.setattr(
        provider,
        "_login_and_get_info",
        lambda url, username=None, password=None, user_id=None: (
            "tok",
            "https://m.example.com",
            {"title": "clip", "original_media_url": "/media/clip.mp4"},
            "AUTH",
        ),
    )
    monkeypatch.setattr(provider, "_get_verify_ssl_for_host", lambda *a, **k: True)
    monkeypatch.setattr(
        mediacms, "_pin_media_url", lambda url, purpose: types.SimpleNamespace(url=url, headers={})
    )

    @contextlib.contextmanager
    def session(target):
        yield types.SimpleNamespace(get=lambda *a, **k: response)

    monkeypatch.setattr(mediacms, "pinned_requests_session", session)
    return provider


def test_a_negative_content_length_reports_no_download_progress(monkeypatch, tmp_path):
    """A negative length is not a usable total: only the starting 20 is reported."""
    provider = _provider_serving(monkeypatch, _StreamedResponse([b"ab", b"cd"], "-5"))
    reported: list[int] = []

    provider.download(
        "https://m.example.com/view?m=tok",
        str(tmp_path),
        progress_callback=lambda percent, _message: reported.append(percent),
    )

    assert reported == [20]


@pytest.mark.parametrize("thumbnail", [5, True, {"src": "/t.jpg"}])
def test_a_non_string_thumbnail_does_not_break_the_media_info(thumbnail):
    """The server's thumbnail value is stringified before it is parsed, never raised on."""
    info = mediacms.MediacmsProvider()._build_media_info(
        "tok", "https://m.example.com", {"thumbnail_url": thumbnail, "title": "t"}, "u"
    )

    assert info["thumbnail"] == "https://m.example.com/" + str(thumbnail)
