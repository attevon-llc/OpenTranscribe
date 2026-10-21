"""``POST /files/process-url`` honours the per-file speaker range and model (issue #1201).

The extraction (yt-dlp, network) and the Celery publish are stubbed; the request model, the
validation and the placeholder row are real.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import status

import app.api.endpoints.files.url_processing as url_processing
from app.models.media import MediaFile

URL = "https://vimeo.com/123456"
DEPLOYMENT_MODEL = "large-v3-turbo"


@pytest.fixture
def url_seams(monkeypatch):
    monkeypatch.setattr(
        url_processing,
        "_extract_video_info",
        lambda *a, **k: ("vid-1201", "A talk", {"extractor": "vimeo", "duration": 60}),
    )
    task_calls: list[dict] = []
    playlist_calls: list[dict] = []

    def _record(into: list[dict]):
        def _delay(**kwargs):
            into.append(kwargs)
            return SimpleNamespace(id="stub-task")

        return _delay

    monkeypatch.setattr(url_processing.process_youtube_url_task, "delay", _record(task_calls))
    monkeypatch.setattr(
        url_processing.process_youtube_playlist_task, "delay", _record(playlist_calls)
    )
    monkeypatch.setattr(
        "app.utils.whisper_model_choice.deployment_model_name", lambda: DEPLOYMENT_MODEL
    )
    return SimpleNamespace(tasks=task_calls, playlists=playlist_calls)


def test_the_range_and_model_are_recorded_on_the_placeholder(
    client, user_token_headers, db_session, url_seams
):
    response = client.post(
        "/api/files/process-url",
        headers=user_token_headers,
        json={
            "url": URL,
            "min_speakers": 3,
            "max_speakers": 5,
            "num_speakers": 4,
            "whisper_model": "tiny",
        },
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    row = db_session.query(MediaFile).filter(MediaFile.uuid == response.json()["uuid"]).one()
    assert (row.requested_min_speakers, row.requested_max_speakers) == (3, 5)
    assert row.requested_num_speakers == 4
    assert row.requested_whisper_model == "tiny"
    assert len(url_seams.tasks) == 1


def test_nothing_entered_records_nothing(client, user_token_headers, db_session, url_seams):
    response = client.post("/api/files/process-url", headers=user_token_headers, json={"url": URL})
    assert response.status_code == status.HTTP_200_OK, response.text
    row = db_session.query(MediaFile).filter(MediaFile.uuid == response.json()["uuid"]).one()
    assert row.requested_min_speakers is None
    assert row.requested_whisper_model is None


@pytest.mark.parametrize(
    "extra",
    [
        {"whisper_model": "medium"},
        {"min_speakers": 6, "max_speakers": 2},
        {"min_speakers": 0},
    ],
    ids=["unservable-model", "min-above-max", "non-positive"],
)
def test_bad_options_are_a_422_and_create_no_file(
    client, user_token_headers, db_session, url_seams, extra
):
    before = db_session.query(MediaFile).count()
    response = client.post(
        "/api/files/process-url", headers=user_token_headers, json={"url": URL, **extra}
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert db_session.query(MediaFile).count() == before
    assert url_seams.tasks == []


def test_a_playlist_carries_the_options_to_its_task(
    client, user_token_headers, url_seams, monkeypatch
):
    monkeypatch.setattr(
        type(url_processing.MediaDownloadService()),
        "is_playlist_url",
        lambda self, url: True,
        raising=False,
    )
    response = client.post(
        "/api/files/process-url",
        headers=user_token_headers,
        json={"url": URL, "min_speakers": 2, "whisper_model": "base"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert url_seams.playlists[0]["transcription_options"] == {
        "min_speakers": 2,
        "max_speakers": None,
        "num_speakers": None,
        "whisper_model": "base",
    }
