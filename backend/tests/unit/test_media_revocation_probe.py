"""The S3 startup probe: finds a missing takedown permission at boot, never blocks it.

Runs ``media_revocation_probe`` against ``fixtures.fake_tagging_store`` (real ``S3Error``).
The live counterpart with a real under-privileged MinIO identity is
``integration/test_quarantine_tag_permissions_live.py``.
"""

from __future__ import annotations

import pytest
from fixtures.fake_tagging_store import FakeTaggingStore

from app.core.config import settings
from app.services import media_revocation_probe as probe_mod

MEDIA = "media-under-test"
CACHE = "cache-under-test"
GET_TAGS = "s3:GetObjectTagging"
PUT_TAGS = "s3:PutObjectTagging"


@pytest.fixture(autouse=True)
def _buckets(monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_BUCKET_NAME", MEDIA)
    monkeypatch.setattr(settings, "CACHE_BUCKET_NAME", CACHE)
    monkeypatch.setattr(settings, "STORAGE_TAG_PROBE_ENABLED", True)


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]


def test_missing_put_object_tagging_is_an_error_naming_it_and_the_bucket(caplog):
    store = FakeTaggingStore(deny=[PUT_TAGS])

    with caplog.at_level("INFO", logger="app.services.media_revocation_probe"):
        probe = probe_mod.report_native_s3_tagging_posture(store)

    assert probe is not None and probe.verified is False
    assert probe.missing_permissions == (f"{PUT_TAGS} on {MEDIA}",)
    assert store.objects == {}
    (error,) = _errors(caplog)
    assert f"{PUT_TAGS} on {MEDIA}" in error
    assert GET_TAGS not in error
    assert "prefix" in error


def test_a_cache_bucket_without_list_permission_is_named():
    probe = probe_mod.probe_takedown_permissions(
        FakeTaggingStore(deny=["s3:ListBucket"]), MEDIA, CACHE
    )

    assert probe.missing_permissions == (f"s3:ListBucket on {CACHE}",)


def test_a_capable_identity_verifies_and_leaves_nothing_behind(caplog):
    store = FakeTaggingStore()

    with caplog.at_level("INFO", logger="app.services.media_revocation_probe"):
        probe = probe_mod.report_native_s3_tagging_posture(store)

    assert probe == probe_mod.TaggingProbe(True)
    assert store.objects == {} and store.tags == {}
    actions = [a for a, _ in store.calls]
    assert actions == [
        "s3:PutObject",
        GET_TAGS,
        PUT_TAGS,
        PUT_TAGS,
        "s3:DeleteObject",
        "s3:PutObject",
        "s3:ListBucket",
        "s3:DeleteObject",
    ]
    assert [r for r in caplog.records if r.levelno >= 30] == []


def test_each_boot_uses_a_unique_key_so_replicas_never_collide():
    store = FakeTaggingStore()
    probe_mod.probe_takedown_permissions(store, MEDIA, None)
    probe_mod.probe_takedown_permissions(store, MEDIA, None)

    keys = {key for action, key in store.calls if action == "s3:PutObject"}
    assert len(keys) == 2
    assert all(k.startswith(probe_mod.PROBE_PREFIX) for k in keys)


def test_an_unwritable_probe_object_is_inconclusive_not_an_error(caplog):
    with caplog.at_level("INFO", logger="app.services.media_revocation_probe"):
        probe = probe_mod.report_native_s3_tagging_posture(FakeTaggingStore(deny=["s3:PutObject"]))

    assert probe is not None and probe.verified is False
    assert probe.missing_permissions == ()
    assert probe.error is not None and "AccessDenied" in probe.error
    assert [r.levelname for r in caplog.records if r.levelno >= 30] == ["WARNING"]


def test_the_opt_out_makes_no_storage_call(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_TAG_PROBE_ENABLED", False)
    store = FakeTaggingStore()

    assert probe_mod.report_native_s3_tagging_posture(store) is None
    assert store.calls == []


def test_without_an_override_it_uses_the_bounded_probe_client(monkeypatch):
    store = FakeTaggingStore()
    monkeypatch.setattr("app.services.minio_service.get_probe_client", lambda: store)
    monkeypatch.setattr(
        "app.services.minio_service.minio_client", FakeTaggingStore(deny=["s3:PutObject"])
    )

    probe = probe_mod.report_native_s3_tagging_posture()

    assert probe == probe_mod.TaggingProbe(True)
    assert store.calls, "the probe must run on the probe client, not the shared one"
