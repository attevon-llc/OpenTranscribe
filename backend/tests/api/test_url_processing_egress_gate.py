"""Pins the SSRF/egress gate of ``POST /api/files/process-url`` end to end.

The unit tests in ``tests/unit/test_ssrf_url_validation.py`` pin ``is_safe_url`` itself;
these pin that the endpoint actually routes every submission through it, BEFORE any
metadata extraction, task dispatch or placeholder row, and that the URL it validated is
the URL it later fetches. DNS is stubbed for test hostnames only (literal IPs and
``localhost`` resolve locally), so nothing here touches the network.
"""

from __future__ import annotations

import socket
import uuid
from types import SimpleNamespace

import pytest
from fastapi import status

import app.api.endpoints.files.url_processing as url_processing
import app.api.endpoints.files.url_video_import as url_video_import
import app.utils.url_validation as url_validation
from app.core.config import settings
from app.models.media import MediaFile
from app.services.media_download_service import MediaDownloadService

INVALID = "Invalid URL. Please enter a valid HTTP or HTTPS URL."
PUBLIC_IP = "93.184.216.34"

_real_getaddrinfo = socket.getaddrinfo


@pytest.fixture
def gate(monkeypatch):
    """Record extraction and both dispatch seams; let tests map test hostnames to IPs."""
    monkeypatch.setattr(settings, "YOUTUBE_USER_RATE_LIMIT_ENABLED", False)
    extracted: list[str] = []
    tasks: list[dict] = []
    playlists: list[dict] = []
    dns: dict[str, list[str]] = {}

    def _extract(self, url, **kwargs):
        extracted.append(url)
        return {"id": f"egress-{uuid.uuid4().hex[:10]}", "title": "t", "extractor": "youtube"}

    def _record(into: list[dict]):
        def _delay(**kwargs):
            into.append(kwargs)
            return SimpleNamespace(id="stub-task")

        return _delay

    def _getaddrinfo(host, port, *args, **kwargs):
        if host in dns:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in dns[host]
            ]
        return _real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(MediaDownloadService, "extract_video_info", _extract)
    monkeypatch.setattr(url_video_import.process_youtube_url_task, "delay", _record(tasks))
    monkeypatch.setattr(url_processing.process_youtube_playlist_task, "delay", _record(playlists))
    monkeypatch.setattr(url_validation.socket, "getaddrinfo", _getaddrinfo)
    return SimpleNamespace(extracted=extracted, tasks=tasks, playlists=playlists, dns=dns)


def _post(client, headers, url):
    return client.post("/api/files/process-url", headers=headers, json={"url": url})


def _observed(response, gate, db_session) -> tuple:
    """Status, detail, every fetch/dispatch seam and the row count, for one comparison."""
    return (
        response.status_code,
        response.json().get("detail"),
        gate.extracted,
        gate.tasks,
        gate.playlists,
        db_session.query(MediaFile).count(),
    )


def _refused(before: int) -> tuple:
    return (status.HTTP_400_BAD_REQUEST, INVALID, [], [], [], before)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5/video",
        "http://172.16.0.1/video",
        "http://192.168.1.10/video",
        "http://127.0.0.1:6379/",
        "http://[::1]/video",
        "http://[fd00::1]/video",
        "http://0.0.0.0/video",
        "http://169.254.170.2/v2/credentials",
        "http://metadata.google.internal/computeMetadata/v1/",
        "file:///etc/passwd",
        "gopher://127.0.0.1:70/_x",
        "http://",
        "   ",
    ],
    ids=[
        "rfc1918-10",
        "rfc1918-172",
        "rfc1918-192",
        "loopback-v4-port",
        "loopback-v6",
        "ula-v6",
        "unspecified",
        "link-local-ecs-metadata",
        "metadata-hostname",
        "file-scheme",
        "gopher-scheme",
        "no-host",
        "whitespace-only",
    ],
)
def test_unsafe_url_is_refused_before_any_fetch(client, user_token_headers, db_session, gate, url):
    before = db_session.query(MediaFile).count()
    response = _post(client, user_token_headers, url)
    assert _observed(response, gate, db_session) == _refused(before)


def test_public_looking_name_resolving_to_a_private_address_is_refused(
    client, user_token_headers, db_session, gate
):
    gate.dns["media.egress-gate.test"] = ["10.1.2.3"]
    before = db_session.query(MediaFile).count()
    response = _post(client, user_token_headers, "https://media.egress-gate.test/v/1")
    assert _observed(response, gate, db_session) == _refused(before)


def test_name_with_one_private_record_among_public_ones_is_refused(
    client, user_token_headers, db_session, gate
):
    gate.dns["mixed.egress-gate.test"] = [PUBLIC_IP, "192.168.0.9"]
    before = db_session.query(MediaFile).count()
    response = _post(client, user_token_headers, "https://mixed.egress-gate.test/v/1")
    assert _observed(response, gate, db_session) == _refused(before)


def test_a_playlist_url_is_validated_before_the_playlist_task_is_dispatched(
    client, user_token_headers, db_session, gate
):
    gate.dns["www.youtube.com"] = ["10.9.9.9"]
    before = db_session.query(MediaFile).count()
    response = _post(client, user_token_headers, "https://youtube.com/playlist?list=PLegress")
    assert _observed(response, gate, db_session) == _refused(before)


def test_the_validated_url_is_the_one_extracted_and_dispatched(
    client, user_token_headers, db_session, gate
):
    gate.dns["www.youtube.com"] = [PUBLIC_IP]
    response = _post(client, user_token_headers, "  https://youtu.be/egressAbc12  ")
    assert response.status_code == status.HTTP_200_OK, response.text
    normalized = "https://www.youtube.com/watch?v=egressAbc12"
    assert gate.extracted == [normalized]
    assert [t["url"] for t in gate.tasks] == [normalized]
    row = db_session.query(MediaFile).filter(MediaFile.uuid == response.json()["uuid"]).one()
    assert row.source_url == normalized
    db_session.delete(row)
    db_session.commit()
