"""A URL import's thumbnail is stored under a key owned by its media file (issue #1357)."""

from __future__ import annotations

import re

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from app.services.media_download_service import MediaDownloadService
from tests.helpers import stub_public_dns

_FILE_KEY = re.compile(r"^user_(\d+)/file_(\d+)/")


@pytest.fixture
def uploads(monkeypatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(
        "app.services.media_download_service.upload_file",
        lambda *, file_content, file_size, object_name, content_type: recorded.append(object_name),
    )
    stub_public_dns(monkeypatch, domain="thumbs.example.com")

    def fake_send(self, request, *args, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        resp.headers = CaseInsensitiveDict({})
        resp._content = b"\xff\xd8\xff\xe0jpeg"
        resp._content_consumed = True
        resp.url = request.url or ""
        resp.request = request
        return resp

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    return recorded


def test_two_imports_of_one_video_get_distinct_thumbnail_keys(uploads):
    info = {"thumbnail": "https://thumbs.example.com/t.jpg", "id": "same", "extractor": "Youtube"}
    service = MediaDownloadService()

    first = service._download_media_thumbnail_sync(info, 3, media_file_id=10)
    second = service._download_media_thumbnail_sync(info, 3, media_file_id=11)

    assert first != second
    assert uploads == [first, second]
    for key, file_id in ((first, "10"), (second, "11")):
        match = _FILE_KEY.match(str(key))
        assert match is not None and match.group(2) == file_id
