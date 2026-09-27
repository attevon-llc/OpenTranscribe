"""Unit tests for storage_presign_identity.py (issue #907) — no MinIO needed.

Covers the policy shape (the Deny condition, bucket-vs-object ARNs, excluded
write-side actions), deterministic credential derivation, and the
``presign_client()``/``ensure_presign_identity()`` fallback behavior. All pure-
Python or mocked — the falsifiable live-MinIO test is
``tests/integration/test_presign_revocation.py``.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.core.constants import STORAGE_QUARANTINE_TAG_KEY
from app.services import storage_presign_identity as pid


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Reset per-process memoization so tests don't leak into each other."""
    monkeypatch.setattr(pid, "_ensure_attempted", False)
    monkeypatch.setattr(pid, "_ensure_result", False)
    monkeypatch.setattr(pid, "_restricted_client", None)
    monkeypatch.setattr(pid, "_fallback_error_logged", False)


class TestBuildPresignPolicy:
    def test_deny_targets_get_object_with_the_exact_string_equals_condition(self):
        policy = pid.build_presign_policy(["bucket-a", "bucket-b"], STORAGE_QUARANTINE_TAG_KEY)
        deny = [s for s in policy["Statement"] if s["Effect"] == "Deny"]
        assert len(deny) == 1
        stmt = deny[0]
        assert stmt["Action"] == ["s3:GetObject"]
        assert set(stmt["Resource"]) == {"arn:aws:s3:::bucket-a/*", "arn:aws:s3:::bucket-b/*"}
        assert stmt["Condition"] == {
            "StringEquals": {f"s3:ExistingObjectTag/{STORAGE_QUARANTINE_TAG_KEY}": "true"}
        }

    def test_grants_get_bucket_location_on_bucket_level_arns_not_object_level(self):
        policy = pid.build_presign_policy(["bucket-a", "bucket-b"], STORAGE_QUARANTINE_TAG_KEY)
        loc_statements = [
            s for s in policy["Statement"] if "s3:GetBucketLocation" in s.get("Action", [])
        ]
        assert len(loc_statements) == 1
        resources = loc_statements[0]["Resource"]
        assert set(resources) == {"arn:aws:s3:::bucket-a", "arn:aws:s3:::bucket-b"}
        # Presigning itself needs this at the BUCKET level (minio-py's internal
        # _get_region() call), not the object level — measured, bit the research
        # probe on the first attempt.
        assert all(not r.endswith("/*") for r in resources)

    def test_no_tagging_or_list_bucket_rights_anywhere_in_the_policy(self):
        policy = pid.build_presign_policy(["bucket-a"], STORAGE_QUARANTINE_TAG_KEY)
        all_actions = {a for stmt in policy["Statement"] for a in stmt.get("Action", [])}
        forbidden = {"s3:PutObjectTagging", "s3:DeleteObjectTagging", "s3:ListBucket"}
        assert not (all_actions & forbidden), (
            "the identity a Deny is keyed on must not be able to clear its own tag "
            "or enumerate the bucket (measured least-privilege containment)"
        )

    def test_no_existing_object_tag_condition_on_any_write_action(self):
        # MinIO rejects an ExistingObjectTag condition on a write action (400s the
        # whole policy-creation call) — s3:PutObject must never appear in the Deny.
        policy = pid.build_presign_policy(["bucket-a"], STORAGE_QUARANTINE_TAG_KEY)
        write_actions = {"s3:PutObject", "s3:PutObjectTagging", "s3:DeleteObjectTagging"}

        conditioned_statements = [s for s in policy["Statement"] if "Condition" in s]
        # Unconditional: the policy must actually contain a conditioned statement (the
        # Deny) or this assertion would pass vacuously against an empty policy.
        assert len(conditioned_statements) == 1

        conditioned_actions = {a for s in conditioned_statements for a in s.get("Action", [])}
        assert not (conditioned_actions & write_actions)


class TestDerivePresignCredentials:
    def test_deterministic_for_a_fixed_jwt_secret(self, monkeypatch):
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "fixed-secret-for-test-one")
        first = pid.derive_presign_credentials()
        second = pid.derive_presign_credentials()
        assert first == second

    def test_differs_for_a_different_jwt_secret(self, monkeypatch):
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "secret-alpha")
        one = pid.derive_presign_credentials()
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "secret-beta")
        two = pid.derive_presign_credentials()
        assert one != two

    def test_key_lengths_and_base32_validity(self, monkeypatch):
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "another-fixed-secret-value")
        access_key, secret_key = pid.derive_presign_credentials()
        assert len(access_key) == 20
        assert len(secret_key) == 40
        # base32's alphabet is A-Z2-7; the trailing '=' padding was stripped, so pad
        # back out to a multiple of 8 before decoding to validate the alphabet.
        for value in (access_key, secret_key):
            padded = value + "=" * (-len(value) % 8)
            base64.b32decode(padded)  # raises binascii.Error if invalid


def test_quarantine_tag_value_is_exactly_lowercase_true():
    # MEASURED: StringEquals is CASE-SENSITIVE — "TRUE" does NOT trigger the Deny.
    # Every tagger must write exactly this value; don't "normalize" it elsewhere.
    assert pid.QUARANTINE_TAG_VALUE == "true"


class TestPresignClientFallback:
    @pytest.fixture(autouse=True)
    def _minio_backend_enabled(self, monkeypatch):
        # The ERROR is for a provisioning FAILURE, which only exists on MinIO with the
        # feature on — pin both so these tests don't depend on the ambient config.
        from app.services import storage_backend

        monkeypatch.setattr(storage_backend, "is_native_s3", lambda: False)
        monkeypatch.setattr(settings, "STORAGE_PRESIGN_IDENTITY_ENABLED", True)

    def test_returns_root_client_and_logs_error_when_identity_unavailable(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(pid, "ensure_presign_identity", lambda: False)
        from app.services.minio_service import minio_client as root_client

        with caplog.at_level("ERROR", logger="app.services.storage_presign_identity"):
            client = pid.presign_client()

        assert client is root_client
        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_logs_error_only_once_per_process(self, monkeypatch, caplog):
        monkeypatch.setattr(pid, "ensure_presign_identity", lambda: False)

        with caplog.at_level("ERROR", logger="app.services.storage_presign_identity"):
            pid.presign_client()
            pid.presign_client()
            pid.presign_client()

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_records) == 1

    def test_returns_restricted_client_when_identity_available(self, monkeypatch):
        monkeypatch.setattr(pid, "ensure_presign_identity", lambda: True)
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "restricted-client-test-secret")
        from app.services.minio_service import minio_client as root_client

        client = pid.presign_client()

        assert client is not root_client


class TestExpectedFallbacksAreNotErrors:
    """Issue #1005: native S3 and disabled-by-config are expected, not faults.

    Both used to log a MinIO-worded ERROR from every process on its first presign.
    They're reported once at startup instead; the presign path stays quiet.
    """

    def test_native_s3_presigns_with_the_storage_client_and_logs_no_error(
        self, monkeypatch, caplog
    ):
        from app.services import storage_backend
        from app.services.minio_service import minio_client as root_client

        monkeypatch.setattr(storage_backend, "is_native_s3", lambda: True)
        monkeypatch.setattr(settings, "STORAGE_PRESIGN_IDENTITY_ENABLED", True)

        with caplog.at_level("DEBUG", logger="app.services.storage_presign_identity"):
            clients = [pid.presign_client() for _ in range(3)]

        assert all(c is root_client for c in clients)
        assert [r for r in caplog.records if r.levelno >= 30] == []
        assert not any("MinIO" in r.getMessage() for r in caplog.records)

    def test_disabled_identity_logs_no_error_on_presign(self, monkeypatch, caplog):
        from app.services import storage_backend

        monkeypatch.setattr(storage_backend, "is_native_s3", lambda: False)
        monkeypatch.setattr(settings, "STORAGE_PRESIGN_IDENTITY_ENABLED", False)

        with caplog.at_level("DEBUG", logger="app.services.storage_presign_identity"):
            pid.presign_client()

        assert [r for r in caplog.records if r.levelno >= 30] == []


def _documented_s3_policy(bucket: str) -> str:
    """The bucket policy ``docs/abuse-and-takedown.md`` tells an S3 operator to attach."""
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{bucket}/*",
                    "Condition": {
                        "StringEquals": {
                            f"s3:ExistingObjectTag/{STORAGE_QUARANTINE_TAG_KEY}": "true"
                        }
                    },
                }
            ],
        }
    )


def _mutated_policy(bucket: str, **overrides) -> str:
    policy = json.loads(_documented_s3_policy(bucket))
    policy["Statement"][0].update(overrides)
    return json.dumps(policy)


class TestBucketPolicyDetection:
    def test_the_documented_policy_is_recognised(self):
        assert pid.bucket_policy_denies_quarantined_reads(_documented_s3_policy("media"), "media")

    def test_it_is_recognised_alongside_unrelated_statements(self):
        policy = json.loads(_documented_s3_policy("media"))
        policy["Statement"].insert(
            0,
            {
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": "arn:aws:s3:::media/*",
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
        )
        assert pid.bucket_policy_denies_quarantined_reads(json.dumps(policy), "media")

    @pytest.mark.parametrize(
        "policy_json",
        [
            None,
            "",
            "not json",
            _mutated_policy("media", Effect="Allow"),
            _mutated_policy("media", Principal={"AWS": "arn:aws:iam::123456789012:role/x"}),
            _mutated_policy("media", Action="s3:PutObject"),
            _mutated_policy("media", Resource="arn:aws:s3:::other-bucket/*"),
            _mutated_policy(
                "media",
                Condition={
                    "StringEquals": {f"s3:ExistingObjectTag/{STORAGE_QUARANTINE_TAG_KEY}": "TRUE"}
                },
            ),
            _mutated_policy(
                "media", Condition={"StringEquals": {"s3:ExistingObjectTag/other-tag": "true"}}
            ),
        ],
        ids=[
            "none",
            "empty",
            "garbage",
            "allow",
            "scoped-principal",
            "wrong-action",
            "wrong-bucket",
            "case-mismatched-value",
            "wrong-tag-key",
        ],
    )
    def test_near_misses_are_not_counted_as_enforced(self, policy_json):
        assert pid.bucket_policy_denies_quarantined_reads(policy_json, "media") is False


class _PolicyClient:
    """Storage client stand-in answering ``get_bucket_policy`` like minio-py does."""

    def __init__(self, policy_json: str | None = None, error_code: str | None = None):
        self._policy_json = policy_json
        self._error_code = error_code
        self.asked_for: list[str] = []

    def get_bucket_policy(self, bucket_name: str) -> str:
        from minio.error import S3Error

        self.asked_for.append(bucket_name)
        if self._error_code:
            raise S3Error(MagicMock(), self._error_code, "msg", bucket_name, "req", "host")
        return self._policy_json or ""


class TestNativeS3RevocationPosture:
    @pytest.fixture(autouse=True)
    def _bucket(self, monkeypatch):
        monkeypatch.setattr(settings, "MEDIA_BUCKET_NAME", "media")
        monkeypatch.setattr(settings, "MEDIA_URL_EXPIRE_SECONDS", 1800)

    def _warnings(self, caplog):
        return [r for r in caplog.records if r.levelname == "WARNING"]

    def test_attached_policy_reports_enforced_without_warning(self, caplog):
        client = _PolicyClient(_documented_s3_policy("media"))

        with caplog.at_level("INFO", logger="app.services.storage_presign_identity"):
            enforced = pid.report_native_s3_revocation_posture(client)

        assert enforced is True
        assert client.asked_for == ["media"]
        assert self._warnings(caplog) == []

    def test_missing_policy_warns_once_naming_the_ttl_window(self, caplog):
        client = _PolicyClient(error_code="NoSuchBucketPolicy")

        with caplog.at_level("INFO", logger="app.services.storage_presign_identity"):
            enforced = pid.report_native_s3_revocation_posture(client)

        assert enforced is False
        warnings = self._warnings(caplog)
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "MEDIA_URL_EXPIRE_SECONDS=1800s" in message
        assert "docs/abuse-and-takedown.md" in message
        assert "MinIO" not in message
        assert not any(r.levelno >= 40 for r in caplog.records)

    def test_policy_without_the_deny_warns(self, caplog):
        client = _PolicyClient(_mutated_policy("media", Effect="Allow"))

        with caplog.at_level("INFO", logger="app.services.storage_presign_identity"):
            enforced = pid.report_native_s3_revocation_posture(client)

        assert enforced is False
        assert len(self._warnings(caplog)) == 1

    def test_unreadable_policy_warns_that_it_could_not_verify(self, caplog):
        client = _PolicyClient(error_code="AccessDenied")

        with caplog.at_level("INFO", logger="app.services.storage_presign_identity"):
            enforced = pid.report_native_s3_revocation_posture(client)

        assert enforced is False
        warnings = self._warnings(caplog)
        assert len(warnings) == 1
        assert "could not read the bucket policy" in warnings[0].getMessage()
        assert "AccessDenied" in warnings[0].getMessage()


class TestEnsurePresignIdentityGating:
    def test_disabled_flag_short_circuits_without_an_admin_call(self, monkeypatch):
        monkeypatch.setattr(settings, "STORAGE_PRESIGN_IDENTITY_ENABLED", False)
        mock_admin_cls = MagicMock()
        monkeypatch.setattr("minio.minioadmin.MinioAdmin", mock_admin_cls)

        result = pid.ensure_presign_identity()

        assert result is False
        mock_admin_cls.assert_not_called()

    def test_native_s3_short_circuits_without_an_admin_call(self, monkeypatch):
        from app.services import storage_backend

        monkeypatch.setattr(storage_backend, "is_native_s3", lambda: True)
        mock_admin_cls = MagicMock()
        monkeypatch.setattr("minio.minioadmin.MinioAdmin", mock_admin_cls)

        result = pid.ensure_presign_identity()

        assert result is False
        mock_admin_cls.assert_not_called()

    def test_result_is_memoized_after_the_first_call(self, monkeypatch):
        from app.services import storage_backend

        monkeypatch.setattr(storage_backend, "is_native_s3", lambda: True)

        first = pid.ensure_presign_identity()
        # Flip the underlying condition; the memoized result must NOT change.
        monkeypatch.setattr(storage_backend, "is_native_s3", lambda: False)
        second = pid.ensure_presign_identity()

        assert first == second is False
