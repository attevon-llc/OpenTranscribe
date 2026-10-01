"""Queue routing for ``POST /api/speaker-clusters/recluster`` (issue #1083).

Re-clustering only runs its similarity math on CUDA once a tenant partition reaches
``SPEAKER_CLUSTERING_GPU_MIN_SPEAKERS`` speakers; below that the GPU worker runs it
on its CPU. Publishing a small recluster to ``gpu`` therefore bought nothing and, on
a deployment whose GPU workers scale to zero, forced a GPU cold start for an
interactive click. The router and the service now read ONE constant, so they cannot
drift.

Each test asserts on the ``queue=`` kwarg of the task's ``apply_async`` (the task
itself is mocked, so no broker is touched).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import status

from app.core import constants
from app.models.media import MediaFile
from app.models.media import Speaker

URL = "/api/speaker-clusters/recluster"


def _add_unlabeled_speakers(db_session, owner, count: int, organization_id=None) -> None:
    mf = MediaFile(
        user_id=owner.id,
        organization_id=organization_id,
        filename="recluster-routing.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    for i in range(count):
        db_session.add(Speaker(user_id=owner.id, media_file_id=mf.id, name=f"SPEAKER_{i:02d}"))
    db_session.commit()


@pytest.fixture
def mock_recluster_task(monkeypatch):
    from app.tasks import speaker_clustering

    task = MagicMock()
    task.apply_async.return_value = MagicMock(id="routing-task-id")
    task.delay.return_value = MagicMock(id="routing-task-id")
    monkeypatch.setattr(speaker_clustering, "recluster_all_speakers", task)
    return task


def _published_queue(task: MagicMock) -> str | None:
    assert not task.delay.called, "recluster must be published with an explicit queue"
    assert task.apply_async.call_count == 1
    queue = task.apply_async.call_args.kwargs.get("queue")
    return str(queue) if queue is not None else None


def test_small_speaker_set_is_published_to_cpu(
    client, user_token_headers, db_session, normal_user, mock_recluster_task, monkeypatch
):
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    _add_unlabeled_speakers(db_session, normal_user, 3)

    resp = client.post(URL, headers=user_token_headers, json={})

    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert _published_queue(mock_recluster_task) == constants.CeleryQueues.CPU


def test_speaker_set_at_threshold_is_published_to_gpu(
    client, user_token_headers, db_session, normal_user, mock_recluster_task, monkeypatch
):
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    monkeypatch.setattr(constants, "SPEAKER_CLUSTERING_GPU_MIN_SPEAKERS", 4, raising=False)
    _add_unlabeled_speakers(db_session, normal_user, 4)

    resp = client.post(URL, headers=user_token_headers, json={})

    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert _published_queue(mock_recluster_task) == constants.CeleryQueues.GPU


def test_threshold_is_per_tenant_partition_not_user_total(
    client, user_token_headers, db_session, normal_user, mock_recluster_task, monkeypatch
):
    """The service builds one similarity matrix per tenant partition (the speaker's
    file organization), so 3 personal + 3 org speakers is two 3x3 problems, both
    below a threshold of 4. Routing on the user's total (6) would pick the GPU for
    work that runs on CPU."""
    from app.models.organization import Organization

    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    monkeypatch.setattr(constants, "SPEAKER_CLUSTERING_GPU_MIN_SPEAKERS", 4, raising=False)
    org = Organization(name=f"recluster-routing-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    _add_unlabeled_speakers(db_session, normal_user, 3)
    _add_unlabeled_speakers(db_session, normal_user, 3, organization_id=org.id)

    resp = client.post(URL, headers=user_token_headers, json={})

    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert _published_queue(mock_recluster_task) == constants.CeleryQueues.CPU


def test_lite_mode_large_set_still_published_to_cpu(
    client, user_token_headers, db_session, normal_user, mock_recluster_task, monkeypatch
):
    """Lite has no GPU consumer at all (issue #865): size never routes to ``gpu``."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "lite")
    monkeypatch.setattr(constants, "SPEAKER_CLUSTERING_GPU_MIN_SPEAKERS", 4, raising=False)
    _add_unlabeled_speakers(db_session, normal_user, 4)

    resp = client.post(URL, headers=user_token_headers, json={})

    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert _published_queue(mock_recluster_task) == constants.CeleryQueues.CPU


def test_clustering_service_reads_the_shared_threshold(monkeypatch):
    """The service's CUDA switch must use the same constant as the router."""
    import torch

    from app.services.speaker_clustering_service import SpeakerClusteringService

    devices: list[str] = []
    real_tensor = torch.tensor

    def _capture_tensor(data, *args, **kwargs):
        devices.append(str(kwargs.get("device")))
        kwargs["device"] = torch.device("cpu")
        kwargs["dtype"] = torch.float32
        return real_tensor(data, *args, **kwargs)

    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: None)
    monkeypatch.setattr(torch.cuda, "ipc_collect", lambda: None)
    monkeypatch.setattr(torch, "tensor", _capture_tensor)
    monkeypatch.setattr(constants, "SPEAKER_CLUSTERING_GPU_MIN_SPEAKERS", 3, raising=False)

    svc = SpeakerClusteringService.__new__(SpeakerClusteringService)
    rows = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]]
    svc._compute_similarity_groups(rows, [1, 2, 3], 0.5, lambda *a, **k: None)
    assert devices == ["cuda:0"]

    devices.clear()
    svc._compute_similarity_groups(rows[:2], [1, 2], 0.5, lambda *a, **k: None)
    assert devices == ["cpu"]
