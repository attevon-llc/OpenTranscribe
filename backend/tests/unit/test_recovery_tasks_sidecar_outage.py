"""A sidecar-storage outage must fail/retry the recovery tasks, not read as "empty sidecar".

Before, ``load_metadata_sidecar`` returned ``{}`` for an outage, so ``youtube_metadata_fetch``
re-fetched every id from YouTube (risking the IP block the module rate-limits itself against)
and then overwrote the stored sidecar with the partial set from that run.
"""

from __future__ import annotations

import pytest

from app.services import storage_recovery_service as real
from app.tasks import recovery_tasks


class _InMemoryMinio:
    """Stand-in MinIOService whose sidecar object can be made unreachable."""

    def __init__(self, stored: bytes, outage: Exception | None) -> None:
        self.stored = stored
        self.outage = outage
        self.uploads: list[bytes] = []

    def get_object(self, _bucket, _name):
        if self.outage is not None:
            raise Exception("Error getting object") from self.outage
        stored = self.stored

        class _Resp:
            def read(self):
                return stored

            def close(self):
                pass

            def release_conn(self):
                pass

        return _Resp()

    def upload_bytes(self, *, data, **_kw):
        self.uploads.append(data)
        self.stored = data


def _outage() -> Exception:
    import urllib3
    from urllib3.exceptions import MaxRetryError

    return MaxRetryError(urllib3.HTTPConnectionPool("minio", 9000), "/x", None)


def test_fetch_does_not_overwrite_the_stored_sidecar_when_the_load_hits_an_outage(monkeypatch):
    """Outage at load time used to give ``cache == {}``: every id was re-fetched from
    YouTube and the stored sidecar was then replaced by the partial set from this run."""
    import app.services.minio_service as minio_service

    stored = b'{"vid1": {"title": "Kept", "duration": 10}}'
    fake = _InMemoryMinio(stored, _outage())
    monkeypatch.setattr("app.services.minio_bucket_admin.MinIOService", lambda: fake)
    monkeypatch.setattr(recovery_tasks, "_YT_METADATA_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(minio_service, "minio_client", object(), raising=False)
    monkeypatch.setattr(
        recovery_tasks.recovery, "discover_youtube_ids", lambda *_a: ["vid1", "vid2"]
    )
    fetch_calls: list[str] = []

    def _fetch(vid: str) -> dict[str, object]:
        fetch_calls.append(vid)
        return {"title": "New", "duration": 5}

    monkeypatch.setattr(recovery_tasks.recovery, "fetch_youtube_metadata", _fetch)

    with pytest.raises(real.MetadataSidecarUnavailableError):
        recovery_tasks.youtube_metadata_fetch(user_id=1)

    assert fetch_calls == []
    assert fake.uploads == []
    assert fake.stored == stored


def test_backfill_raises_on_a_sidecar_outage_instead_of_reporting_an_empty_success(
    monkeypatch,
):

    fake = _InMemoryMinio(b"{}", _outage())
    monkeypatch.setattr("app.services.minio_bucket_admin.MinIOService", lambda: fake)
    monkeypatch.setattr(
        recovery_tasks.recovery, "load_metadata_sidecar", real.load_metadata_sidecar
    )

    with pytest.raises(real.MetadataSidecarUnavailableError):
        recovery_tasks.youtube_metadata_backfill(user_id=1)


def test_fetch_requests_a_celery_retry_on_a_sidecar_outage(monkeypatch):
    """Through the task's own retry hook, not by swallowing the error."""
    import app.services.minio_service as minio_service

    monkeypatch.setattr("app.services.minio_bucket_admin.MinIOService", lambda: object())
    monkeypatch.setattr(minio_service, "minio_client", object(), raising=False)
    monkeypatch.setattr(recovery_tasks.recovery, "discover_youtube_ids", lambda *_a: ["v"])

    def _load(_svc):
        raise real.MetadataSidecarUnavailableError("down")

    monkeypatch.setattr(recovery_tasks.recovery, "load_metadata_sidecar", _load)
    retries: list[Exception] = []

    def _retry(*_a, exc=None, **_k):
        retries.append(exc)
        return exc

    monkeypatch.setattr(recovery_tasks.youtube_metadata_fetch, "retry", _retry)

    with pytest.raises(real.MetadataSidecarUnavailableError):
        recovery_tasks.youtube_metadata_fetch(user_id=1)

    assert len(retries) == 1
    assert isinstance(retries[0], real.MetadataSidecarUnavailableError)
