"""Unit tests for storage_presign_identity.py (issue #907) — no MinIO needed.

Covers the policy shape (the Deny condition, bucket-vs-object ARNs, excluded
write-side actions), deterministic credential derivation, and the
``presign_client()``/``ensure_presign_identity()`` fallback behavior. All pure-
Python or mocked — the falsifiable live-MinIO test is
``tests/integration/test_presign_revocation.py``.
"""

from __future__ import annotations

import base64
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
