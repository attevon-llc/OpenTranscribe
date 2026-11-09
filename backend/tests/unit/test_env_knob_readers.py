"""Worker tuning knobs read through ``app.core.env`` honour their variable, default and floor."""

from __future__ import annotations

import pytest

from app.services.backup_recovery import collect_key_material
from app.services.redaction import pii_pool
from app.services.speaker_attribute_service import DEFAULT_MAX_CLIP_SECONDS
from app.services.speaker_attribute_service import max_clip_seconds
from app.tasks.speaker_attribute_inference import _max_concurrency


def _set(monkeypatch: pytest.MonkeyPatch, name: str, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"), [(None, 2), ("5", 5), ("0", 0), ("junk", 2), ("-3", 0)]
)
def test_speaker_attribute_concurrency(monkeypatch, value, expected):
    _set(monkeypatch, "SPEAKER_ATTRIBUTE_MAX_CONCURRENCY", value)
    assert _max_concurrency() == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, DEFAULT_MAX_CLIP_SECONDS), ("30", 30.0), ("1", DEFAULT_MAX_CLIP_SECONDS), ("x", 20.0)],
)
def test_speaker_attribute_clip_seconds(monkeypatch, value, expected):
    _set(monkeypatch, "SPEAKER_ATTRIBUTE_MAX_CLIP_SECONDS", value)
    assert max_clip_seconds() == expected


@pytest.mark.unit
@pytest.mark.parametrize(("value", "expected"), [("3", 3), ("500", pii_pool.MAX_WORKERS)])
def test_pii_worker_count_is_honoured_and_capped(monkeypatch, value, expected):
    _set(monkeypatch, "REDACTION_PII_WORKERS", value)
    assert pii_pool._worker_count() == expected


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "junk", "0"])
def test_pii_worker_count_falls_back_to_the_cpu_derived_default(monkeypatch, value):
    _set(monkeypatch, "REDACTION_PII_WORKERS", value)
    assert 1 <= pii_pool._worker_count() <= min(pii_pool.DEFAULT_WORKERS, pii_pool.MAX_WORKERS)


@pytest.mark.unit
def test_minio_kms_key_is_collected_only_when_set(monkeypatch):
    _set(monkeypatch, "MINIO_KMS_SECRET_KEY", "  kms-secret  ")
    assert collect_key_material()["MINIO_KMS_SECRET_KEY"] == "kms-secret"
    _set(monkeypatch, "MINIO_KMS_SECRET_KEY", None)
    assert "MINIO_KMS_SECRET_KEY" not in collect_key_material()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"), [(None, False), ("true", True), ("TRUE", True), ("1", False)]
)
def test_gpu_split_flag_requires_the_literal_true(monkeypatch, value, expected):
    from app.core.queues import gpu_split_enabled

    _set(monkeypatch, "ENGINE_GPU_SPLIT", value)
    assert gpu_split_enabled() is expected


@pytest.mark.unit
def test_build_stamps_come_from_the_environment_and_default_to_unknown(monkeypatch):
    from app.core import version

    _set(monkeypatch, "GIT_SHA", " abc1234 ")
    _set(monkeypatch, "BUILD_TIME", None)
    assert version._read_git_sha() == "abc1234"
    assert version._read_build_time() == version.UNKNOWN


@pytest.mark.unit
def test_shared_volume_path_defaults_when_unset(monkeypatch):
    from app.core.scratch_paths import ENGINE_SHARED_VOLUME_DEFAULT
    from app.core.scratch_paths import resolve_engine_shared_volume_path

    _set(monkeypatch, "ENGINE_SHARED_VOLUME_PATH", None)
    assert resolve_engine_shared_volume_path() == ENGINE_SHARED_VOLUME_DEFAULT
