"""Callers of the strict OpenSearch speaker readers (broad-except lane B10).

An outage must not read as "nothing stored": a blended profile centroid was overwritten with
a single embedding, a reassigned segment became the speaker's whole voiceprint, and a gender
analysis reported "no outliers". Each test runs the real reader against a cluster whose
every call fails.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np
import pytest
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError

from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.services.opensearch_service import profiles
from app.services.opensearch_service import speaker_read
from app.services.opensearch_service.client import OpenSearchUnavailableError
from app.services.speaker_matching import match_handling
from app.tasks import speaker_embedding_reassignment_task as reassign

_CLIENT_PATH = "app.services.opensearch_service.client.opensearch_client"


def _down_client() -> MagicMock:
    client = MagicMock()
    err = OpenSearchConnectionError("N/A", "cluster down", Exception())
    for name in ("get", "mget", "search", "msearch", "scroll"):
        getattr(client, name).side_effect = err
    return client


@pytest.fixture
def cluster_down():
    with (
        patch(_CLIENT_PATH, _down_client()),
        patch.object(profiles, "ensure_indices_exist"),
        patch.object(profiles, "get_speaker_index", return_value="idx"),
        patch.object(speaker_read, "ensure_indices_exist"),
        patch.object(speaker_read, "get_speaker_index", return_value="idx"),
    ):
        yield


def _profile() -> SpeakerProfile:
    return SpeakerProfile(id=7, uuid="p-uuid", name="Ann", user_id=1, organization_id=None)


def _tracebacks(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.exc_info]


def test_blended_centroid_is_not_overwritten_when_the_index_is_down(cluster_down, caplog):
    profile = _profile()
    stored = MagicMock()
    with (
        patch("app.services.opensearch_service.store_profile_embedding", stored),
        caplog.at_level(logging.ERROR),
        pytest.raises(OpenSearchUnavailableError),
    ):
        match_handling._store_blended_centroid(profile, np.array([1.0, 0.0]), 3, 4)

    stored.assert_not_called()
    assert _tracebacks(caplog)


def test_blended_centroid_blends_with_the_stored_one_when_the_index_answers():
    profile = _profile()
    written: list[list[float]] = []

    def record(**kwargs):
        written.append(kwargs["embedding"])

    with (
        patch(_CLIENT_PATH) as client,
        patch.object(profiles, "ensure_indices_exist"),
        patch.object(profiles, "get_speaker_index", return_value="idx"),
        patch("app.services.opensearch_service.store_profile_embedding", record),
    ):
        client.get.return_value = {"_source": {"embedding": [0.0, 1.0]}}
        match_handling._store_blended_centroid(profile, np.array([1.0, 0.0]), 1, 2)

    assert written == [pytest.approx([2**-0.5, 2**-0.5])]


def test_profile_update_failure_is_reported_with_a_traceback(caplog):
    speaker = Speaker(id=5, profile_id=7, uuid="s-uuid", media_file_id=1, name="SPEAKER_00")
    svc = MagicMock()
    svc._match_is_blocked.return_value = False
    match = {"auto_accept": True, "profile_id": 7, "suggested_name": "Ann", "confidence": 0.9}
    with (
        patch.object(match_handling, "canonical_speaker_label_for_row", return_value="a"),
        patch.object(match_handling, "_apply_auto_accept"),
        patch.object(match_handling, "_store_voiceprint_and_matches"),
        patch.object(match_handling, "_sync_profile_to_index"),
        patch.object(
            match_handling,
            "_blend_into_profile_centroid",
            side_effect=OpenSearchUnavailableError("down"),
        ),
        caplog.at_level(logging.ERROR),
    ):
        result = match_handling.handle_speaker_match(
            svc, speaker, match, np.array([1.0, 0.0]), 1, 1
        )

    assert result["profile_update_failed"] is True
    assert _tracebacks(caplog)


def test_reassignment_does_not_store_one_segment_as_the_whole_voiceprint(cluster_down, caplog):
    plan = {
        "target_speaker": {"id": 1},
        "duration": 1.0,
        "organization_id": None,
    }
    add = MagicMock()
    with (
        patch.object(reassign, "_load_reassignment_plan", return_value=plan),
        patch.object(reassign, "_extract_segment_embedding", return_value=[1.0, 0.0]),
        patch("app.services.opensearch_service.add_speaker_embedding", add),
        caplog.at_level(logging.WARNING),
        pytest.raises(Exception, match="cluster down|unavailable|lookup failed|Retry"),
    ):
        reassign.update_speaker_embedding_on_reassignment.apply(
            args=["seg", "file", "tgt", None, 1], throw=True
        )

    add.assert_not_called()
