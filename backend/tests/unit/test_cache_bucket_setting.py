"""The derived/bulk-export cache bucket must be operator-configurable (issue #985).

``VideoProcessingService.cache_bucket`` was the hardcoded literal ``"processed-videos"``.
S3 bucket names are ONE GLOBAL NAMESPACE, so a deployment whose credentials cannot reach a
bucket by that name had no way to repoint it — and nothing failed at boot: the service is
constructed per request/task, so bulk subtitle export, subtitle-embedded video download and
the derived-cache admin endpoints each raised at request time out of
``_ensure_cache_bucket_exists``.

Two halves, both covered here, because fixing only the first would create a new broken path:

1. every storage call the service makes targets ``settings.CACHE_BUCKET_NAME``;
2. the presigned-GET identity's policy (issue #907) covers that same configured bucket —
   a policy still scoped to ``processed-videos`` would 403 every presigned derived asset.

The failure-handling contract is deliberately unchanged and asserted below:
``_ensure_cache_bucket_exists`` still re-raises. Configurability is the fix; swallowing the
error would turn a loud misconfiguration into a silent one.
"""

from __future__ import annotations

from typing import cast

import pytest

from app.core.config import DEFAULT_CACHE_BUCKET_NAME
from app.core.config import settings
from app.core.constants import STORAGE_QUARANTINE_TAG_KEY
from app.services import storage_presign_identity as pid
from app.services.minio_service import MinIOService
from app.services.video_processing_service import VideoProcessingService

#: Deliberately unlike the shipped default, so a test can only pass by reading the setting.
CONFIGURED_BUCKET = "acme-ot-derived-cache"


class _RecordingStorage:
    """Stand-in for ``MinIOService`` that records the buckets it is asked to act on.

    Only the four methods ``VideoProcessingService.__init__`` reaches for. Real state
    (which bucket name arrived at each call) is what the tests assert on.
    """

    def __init__(self, *, exists: bool = True, raises: Exception | None = None) -> None:
        self._exists = exists
        self._raises = raises
        self.checked: list[str] = []
        self.created: list[str] = []
        self.expiry_rules: list[tuple[str, str, int, str]] = []

    def bucket_exists(self, bucket_name: str) -> bool:
        if self._raises is not None:
            raise self._raises
        self.checked.append(bucket_name)
        return self._exists

    def make_bucket(self, bucket_name: str) -> None:
        self.created.append(bucket_name)

    def ensure_prefix_expiry(
        self,
        bucket_name: str,
        prefix: str,
        days: int,
        rule_id: str,
        update_if_exists: bool = True,
    ) -> None:
        self.expiry_rules.append((bucket_name, prefix, days, rule_id))

    def remove_lifecycle_rule(self, bucket_name: str, rule_id: str) -> None:
        self.expiry_rules.append((bucket_name, "", 0, rule_id))


def _service(storage: _RecordingStorage) -> VideoProcessingService:
    """Construct the service against the stand-in (one cast, not one per test)."""
    return VideoProcessingService(cast(MinIOService, storage))


@pytest.fixture
def configured_cache_bucket(monkeypatch):
    """Point the cache bucket somewhere other than the shipped default."""
    monkeypatch.setattr(settings, "CACHE_BUCKET_NAME", CONFIGURED_BUCKET)
    return CONFIGURED_BUCKET


class TestServiceReadsTheSetting:
    def test_existing_deployments_keep_the_historical_bucket(self):
        """The shipped default is the old literal, so an upgrade changes no bucket."""
        assert DEFAULT_CACHE_BUCKET_NAME == "processed-videos"

    def test_every_startup_storage_call_targets_the_configured_bucket(
        self, configured_cache_bucket
    ):
        storage = _RecordingStorage()

        service = _service(storage)

        assert service.cache_bucket == configured_cache_bucket
        assert storage.checked == [configured_cache_bucket]
        assert storage.expiry_rules, "expected the bulk-export expiry rule to be applied"
        assert {rule[0] for rule in storage.expiry_rules} == {configured_cache_bucket}

    def test_a_missing_bucket_is_created_under_the_configured_name(self, configured_cache_bucket):
        storage = _RecordingStorage(exists=False)

        _service(storage)

        assert storage.created == [configured_cache_bucket]

    def test_bulk_export_prefix_rule_still_lands_on_that_bucket(self, configured_cache_bucket):
        """``bulk/{job_id}.zip`` (the export archives) must stay covered by the 1-day rule."""
        storage = _RecordingStorage()

        _service(storage)

        bulk = [rule for rule in storage.expiry_rules if rule[3] == "expire-bulk-exports"]
        assert bulk == [(configured_cache_bucket, "bulk/", 1, "expire-bulk-exports")]

    def test_retention_reapplication_targets_the_configured_bucket(self, configured_cache_bucket):
        storage = _RecordingStorage()
        service = _service(storage)
        storage.expiry_rules.clear()

        service.apply_derived_retention(3)

        assert storage.expiry_rules == [
            (
                configured_cache_bucket,
                VideoProcessingService.DERIVED_CACHE_PREFIX,
                3,
                "expire-derived-cache",
            )
        ]

    def test_bucket_failure_still_propagates(self, configured_cache_bucket):
        """Unchanged contract: a bucket that cannot be reached/created raises, not warns."""
        storage = _RecordingStorage(raises=RuntimeError("bucket unreachable"))

        with pytest.raises(RuntimeError, match="bucket unreachable"):
            _service(storage)

        assert storage.created == []


class TestPresignPolicyFollowsTheSetting:
    def test_policy_buckets_are_both_read_from_settings(self, configured_cache_bucket):
        assert pid._presign_buckets() == [settings.MEDIA_BUCKET_NAME, configured_cache_bucket]

    def test_quarantine_deny_covers_the_configured_cache_bucket(self, configured_cache_bucket):
        """Otherwise a takedown could not revoke a presigned derived-asset URL."""
        policy = pid.build_presign_policy(pid._presign_buckets(), STORAGE_QUARANTINE_TAG_KEY)

        deny = [stmt for stmt in policy["Statement"] if stmt["Effect"] == "Deny"]
        assert len(deny) == 1
        assert f"arn:aws:s3:::{configured_cache_bucket}/*" in deny[0]["Resource"]


class TestEnvResolution:
    """Resolved in a clean child process: the ``settings`` singleton binds once at import."""

    _PRINT = "from app.core.config import Settings; print(Settings().CACHE_BUCKET_NAME)"

    def test_env_var_is_honoured(self, run_in_clean_process, tmp_path):
        out = run_in_clean_process(
            self._PRINT,
            CACHE_BUCKET_NAME="ot-prod-cache",
            UPLOAD_DIR=str(tmp_path / "up"),
            TEMP_DIR=str(tmp_path / "tmp"),
        )
        assert out == "ot-prod-cache"

    def test_unset_falls_back_to_the_shipped_default(self, run_in_clean_process, tmp_path):
        out = run_in_clean_process(
            self._PRINT,
            unset=("CACHE_BUCKET_NAME",),
            UPLOAD_DIR=str(tmp_path / "up"),
            TEMP_DIR=str(tmp_path / "tmp"),
        )
        assert out == DEFAULT_CACHE_BUCKET_NAME

    def test_explicitly_blank_falls_back_instead_of_resolving_to_empty(
        self, run_in_clean_process, tmp_path
    ):
        """``CACHE_BUCKET_NAME=`` is the ``.env`` convention for "use the coded default".

        The DATABASE_URL/S3_REGION bug class: a class-body ``os.getenv(key, default)``
        would return ``""`` here, and an empty bucket name fails every storage call.
        """
        out = run_in_clean_process(
            self._PRINT,
            CACHE_BUCKET_NAME="",
            UPLOAD_DIR=str(tmp_path / "up"),
            TEMP_DIR=str(tmp_path / "tmp"),
        )
        assert out == DEFAULT_CACHE_BUCKET_NAME
