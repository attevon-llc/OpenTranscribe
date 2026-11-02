"""The recovery metadata fetch builds its yt-dlp options from the shared YouTube set."""

import yt_dlp

from app.services import storage_recovery_service as recovery
from app.services.media_download_formats import YOUTUBE_EXTRACTOR_ARGS
from app.services.media_download_formats import YT_DLP_BASE_OPTS


def test_metadata_fetch_uses_the_shared_options_and_still_downloads_nothing(monkeypatch):
    seen: dict = {}

    class _FakeYDL:
        def __init__(self, opts):
            seen.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, url, download):
            seen["download"] = download
            return {"title": "T", "duration": 12, "uploader": "U"}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)

    record = recovery.fetch_youtube_metadata("abc123")

    assert record is not None
    assert record["title"] == "T"
    assert seen["download"] is False
    assert seen["skip_download"] is True
    assert seen["js_runtimes"] == YT_DLP_BASE_OPTS["js_runtimes"]
    assert seen["extractor_args"] == YOUTUBE_EXTRACTOR_ARGS
    assert seen["socket_timeout"] == 30
