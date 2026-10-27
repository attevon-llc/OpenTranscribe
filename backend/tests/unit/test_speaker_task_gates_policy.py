"""Queued speaker work asks the privacy policy when it RUNS, not when it was queued (#1280).

A label, a merge, a clustering pass or a batch script can sit in a queue while a layer is
switched off. Each of them re-reads the policy for the scope of the record it works on and
leaves the voiceprint plane alone when it says no. Every case is parametrised over ``allowed``
so the same call is also made with the layer on, which is what proves the gate is the reason
nothing happened.
"""

from __future__ import annotations

import contextlib
import importlib
from typing import Any

import pytest

from app.api.endpoints import speaker_update
from app.tasks import speaker_clustering
from app.tasks import speaker_merge_task
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.privacy_switches import note
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


def _bridge(db_session):
    @contextlib.contextmanager
    def scope():
        yield db_session

    return scope


# ---------------------------------------------------------------------------
# Retroactive matching after a label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed", [True, False])
def test_retroactive_matching_reads_voiceprints_only_when_allowed(
    db_session, normal_user, monkeypatch, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    speaker = make_speaker(
        db_session, normal_user, file_in(db_session, normal_user, org), display_name="Alice"
    )
    reads: list[str] = []
    monkeypatch.setattr(
        speaker_update, "get_speaker_embedding", lambda uuid: note(reads, uuid, None)
    )

    result = speaker_update.trigger_retroactive_matching(speaker, db_session)

    assert result == {"auto_applied_count": 0, "suggested_count": 0}
    assert len(reads) == (1 if allowed else 0)


@pytest.mark.parametrize("allowed", [True, False])
def test_a_label_rebuilds_the_profile_voiceprint_only_when_allowed(
    db_session, normal_user, monkeypatch, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    speaker = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org))
    rebuilt: list[Any] = []
    monkeypatch.setattr(
        "app.services.speaker_cleanup.reconcile_speaker_impact",
        lambda db, impact: note(rebuilt, impact, []),
    )

    speaker_update._update_profile_embedding(db_session, speaker.id, None, 1)

    assert len(rebuilt) == (1 if allowed else 0)


# ---------------------------------------------------------------------------
# Speaker merge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed", [True, False])
def test_the_merge_plan_carries_the_policy_answer_for_the_survivors_file(
    db_session, normal_user, monkeypatch, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    survivor = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org))
    db_session.commit()
    monkeypatch.setattr(speaker_merge_task, "session_scope", _bridge(db_session))

    plan = speaker_merge_task._load_merge_plan(str(survivor.uuid))

    assert plan is not None
    assert plan["voiceprints_allowed"] is allowed


@pytest.fixture
def merge_downstream(monkeypatch) -> list[str]:
    """Stand in for everything the merge task does after the plan; returns the averaged sources."""
    from app.api.endpoints import speakers as speakers_module

    averaged: list[str] = []
    monkeypatch.setattr(
        speakers_module, "_merge_speaker_embeddings", lambda src, target: averaged.append(src)
    )
    for name in ("_clear_speaker_video_cache", "_update_opensearch_speaker_merge"):
        monkeypatch.setattr(speakers_module, name, lambda *a, **k: None)
    monkeypatch.setattr(speakers_module, "_reconcile_speakers_after_merge", lambda *a, **k: [])
    monkeypatch.setattr(speakers_module, "_refresh_analytics_after_merge", lambda *a, **k: None)
    monkeypatch.setattr(
        speaker_merge_task, "session_scope", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr("app.utils.websocket_notify.send_ws_event", lambda *a, **k: True)
    monkeypatch.setattr("app.utils.websocket_notify.send_ws_event_for_file", lambda *a, **k: True)
    return averaged


@pytest.mark.parametrize("allowed", [True, False])
def test_the_merge_task_averages_the_two_voiceprints_only_when_allowed(
    monkeypatch, merge_downstream, allowed
):
    plan = {
        "voiceprints_allowed": allowed,
        "id": 1,
        "uuid": "dst-uuid",
        "display_name": None,
        "media_file_uuid": None,
    }
    monkeypatch.setattr(speaker_merge_task, "_load_merge_plan", lambda uuid: plan)

    result = speaker_merge_task.process_speaker_merge_background.run(
        "src-uuid", "dst-uuid", 1, 10, None, None, []
    )

    assert result["status"] == "success"
    assert result["voiceprint_average"] == ("done" if allowed else "skipped_by_policy")
    assert merge_downstream == (["src-uuid"] if allowed else [])


# ---------------------------------------------------------------------------
# Upload-time clustering task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed", [True, False])
def test_the_clustering_task_clusters_a_file_only_when_allowed(
    db_session, normal_user, monkeypatch, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    media_file = file_in(db_session, normal_user, org)
    db_session.commit()
    clustered: list[int] = []
    monkeypatch.setattr(speaker_clustering, "session_scope", _bridge(db_session))
    monkeypatch.setattr(speaker_clustering, "send_ws_event_for_file", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.services.speaker_clustering_service.SpeakerClusteringService"
        ".cluster_speakers_for_file",
        lambda self, media_file_id, user_id: note(clustered, media_file_id, []),
    )

    speaker_clustering.cluster_speakers_for_file.push_request(id="task-cluster-policy")
    try:
        result = speaker_clustering.cluster_speakers_for_file.run(
            str(media_file.uuid), normal_user.id
        )
    finally:
        speaker_clustering.cluster_speakers_for_file.pop_request()

    assert result["status"] == ("completed" if allowed else "skipped")
    if not allowed:
        assert result["reason"] == "skipped_by_policy"
    assert clustered == ([media_file.id] if allowed else [])


# ---------------------------------------------------------------------------
# The manual batch-matching script
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed", [True, False])
def test_the_batch_matching_script_skips_a_speaker_whose_scope_is_off(
    db_session, normal_user, monkeypatch, allowed
):
    script = importlib.import_module("app.scripts.batch_speaker_matching")
    org = make_org(db_session, voiceprints=allowed)
    speaker = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org))
    db_session.commit()
    matched: list[int] = []

    class Session:
        """The test's session with ``close`` disarmed: the script closes what it opens."""

        def __getattr__(self, name):
            return getattr(db_session, name)

        def close(self):
            return None

    class Matcher:
        def __init__(self, db, embedding_service):
            pass

        def find_and_store_speaker_matches(self, speaker_id, embedding, user_id, threshold):
            matched.append(speaker_id)
            return []

    monkeypatch.setattr(script, "get_db", lambda: iter([Session()]))
    monkeypatch.setattr(script, "SpeakerEmbeddingService", lambda: None)
    monkeypatch.setattr(script, "SpeakerMatchingService", Matcher)
    monkeypatch.setattr(script, "get_speaker_embedding", lambda speaker_id: [1.0, 0.0, 0.0, 0.0])

    script.batch_process_speaker_matches()

    assert (speaker.id in matched) is allowed
