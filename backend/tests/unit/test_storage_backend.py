"""Storage-backend selection: MinIO parity + the native-S3 path (issue #284 A1.11/A1.12).

Two things are being pinned here.

**The MinIO path must not have moved.** ``STORAGE_BACKEND`` defaults to ``minio``,
and every assertion in the first section is the behaviour that shipped before the
switch existed: endpoint from ``MINIO_HOST``/``MINIO_PORT``, static root credentials,
path-style addressing, presigned URLs rewritten onto ``/s3``.

**The S3 path builds the right client and URLs.** There is no AWS account in CI (or
on the developer machine this was written on), so the S3 assertions are made against
a locally-signed URL and the client's own resolved configuration — presigning is pure
local crypto and needs no network. Bucket CORS is the one call that would talk to AWS,
so its boto3 client is mocked. **Nothing here proves OpenTranscribe works against real
S3; it proves we ask for the right thing.**
"""

# mypy: disable-error-code="union-attr"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
from __future__ import annotations

import datetime
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.services import storage_backend as sb

# The clamp logs once per distinct requested value via a module-level set; tests that
# assert on clamping would interfere across xdist workers sharing nothing but this file.
pytestmark = pytest.mark.xdist_group("storage_backend")


@pytest.fixture
def minio_backend(monkeypatch):
    """Force the default (bundled MinIO) backend with known endpoint settings."""
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "minio")
    monkeypatch.setattr(settings, "MINIO_HOST", "minio")
    monkeypatch.setattr(settings, "MINIO_PORT", "9000")
    monkeypatch.setattr(settings, "MINIO_SECURE", False)
    monkeypatch.setattr(settings, "MINIO_PUBLIC_URL", "")
    monkeypatch.setattr(settings, "STORAGE_PUBLIC_URL", "")


@pytest.fixture
def s3_backend(monkeypatch):
    """Force the native-S3 backend with static keys (no IAM lookup in tests)."""
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "")
    monkeypatch.setattr(settings, "S3_REGION", "eu-west-2")
    monkeypatch.setattr(settings, "S3_USE_IAM_ROLE", False)
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")  # gitleaks:allow
    monkeypatch.setattr(
        settings,
        "AWS_SECRET_ACCESS_KEY",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # gitleaks:allow
    )
    monkeypatch.setattr(settings, "STORAGE_PUBLIC_URL", "")
    monkeypatch.setattr(settings, "MINIO_PUBLIC_URL", "")


# ---------------------------------------------------------------------------
# MinIO backend — unchanged behaviour
# ---------------------------------------------------------------------------


def test_minio_is_the_default_backend():
    """A stock install has no STORAGE_BACKEND set and must resolve to MinIO."""
    assert settings.STORAGE_BACKEND == "minio"


def test_minio_endpoint_comes_from_minio_settings(minio_backend):
    assert sb.is_native_s3() is False
    assert sb.storage_endpoint() == ("minio:9000", False)


def test_minio_client_uses_static_root_credentials_and_path_style(minio_backend):
    client = sb.build_storage_client()
    base = client._base_url
    assert base._url.geturl() == "http://minio:9000"
    assert base._virtual_style_flag is False
    creds = client._provider.retrieve()
    assert creds.access_key == settings.MINIO_ROOT_USER
    assert creds.secret_key == settings.MINIO_ROOT_PASSWORD


def test_minio_public_base_defaults_to_the_s3_proxy_path(minio_backend):
    """The frontend/nginx contract: an unset public URL means the /s3 proxy path."""
    assert sb.public_base_url() == "/s3"


def test_minio_rewrites_internal_host_to_proxy_path(minio_backend):
    url = "http://minio:9000/opentranscribe/u/1/a.mp4?X-Amz-Signature=abc"
    assert sb.rewrite_public_host(url) == "/s3/opentranscribe/u/1/a.mp4?X-Amz-Signature=abc"


def test_minio_explicit_public_url_wins(minio_backend, monkeypatch):
    monkeypatch.setattr(settings, "MINIO_PUBLIC_URL", "https://media.example.com/")
    assert sb.public_base_url() == "https://media.example.com"
    assert sb.rewrite_public_host("http://minio:9000/x") == "https://media.example.com/x"


def test_storage_public_url_overrides_the_minio_specific_alias(minio_backend, monkeypatch):
    monkeypatch.setattr(settings, "MINIO_PUBLIC_URL", "https://old.example.com")
    monkeypatch.setattr(settings, "STORAGE_PUBLIC_URL", "https://new.example.com")
    assert sb.public_base_url() == "https://new.example.com"


def test_minio_rewrite_matches_https_endpoint_too(minio_backend, monkeypatch):
    """MINIO_SECURE=true used to defeat the rewrite: it only matched http://."""
    monkeypatch.setattr(settings, "MINIO_SECURE", True)
    assert sb.rewrite_public_host("https://minio:9000/x") == "/s3/x"


def test_minio_url_without_the_internal_host_is_untouched(minio_backend):
    assert sb.rewrite_public_host("https://cdn.example.com/x") == "https://cdn.example.com/x"


def test_minio_allows_single_put_for_every_supported_upload_size(minio_backend):
    assert sb.single_put_max_bytes() == sb.MINIO_SINGLE_PUT_MAX_BYTES
    # The application ceiling (MAX_UPLOAD_BYTES) is 15 GB — far below MinIO's limit,
    # so the browser-direct PUT path stays available for every allowed upload.
    assert sb.supports_single_put(15 * 1024**3) is True


# ---------------------------------------------------------------------------
# Native S3 backend
# ---------------------------------------------------------------------------


def test_s3_endpoint_derives_the_regional_aws_host(s3_backend):
    assert sb.is_native_s3() is True
    assert sb.storage_endpoint() == ("s3.eu-west-2.amazonaws.com", True)


def test_s3_explicit_endpoint_url_is_parsed(s3_backend, monkeypatch):
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "https://s3.wasabisys.com")
    assert sb.storage_endpoint() == ("s3.wasabisys.com", True)
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "http://localhost:9444")
    assert sb.storage_endpoint() == ("localhost:9444", False)
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "storage.example.com")
    assert sb.storage_endpoint() == ("storage.example.com", True)


def test_s3_client_uses_virtual_host_addressing_and_the_signing_region(s3_backend):
    """SigV4 against AWS requires virtual-host style and the bucket's real region."""
    client = sb.build_storage_client()
    base = client._base_url
    assert base._virtual_style_flag is True
    assert base.region == "eu-west-2"
    assert base.is_https is True


def test_s3_presigned_url_is_virtual_hosted_and_scoped_to_the_region(s3_backend):
    """Signing is local crypto — no AWS call — so the URL shape can be asserted offline."""
    client = sb.build_storage_client()
    url = client.presigned_get_object("ot-media", "u/1/a.mp4", expires=datetime.timedelta(hours=6))
    assert url.startswith("https://ot-media.s3.eu-west-2.amazonaws.com/u/1/a.mp4?")
    assert "%2Feu-west-2%2Fs3%2Faws4_request" in url
    assert "X-Amz-Signature=" in url


def test_s3_presigned_urls_are_not_rewritten(s3_backend):
    """The signed host is already public; rewriting it would break host binding."""
    assert sb.public_base_url() is None
    url = "https://ot-media.s3.eu-west-2.amazonaws.com/u/1/a.mp4?X-Amz-Signature=abc"
    assert sb.rewrite_public_host(url) == url


def test_s3_public_url_is_honoured_when_explicitly_configured(s3_backend, monkeypatch):
    """An operator fronting the bucket (CloudFront, custom domain) can opt in."""
    monkeypatch.setattr(settings, "STORAGE_PUBLIC_URL", "https://cdn.example.com")
    assert sb.public_base_url() == "https://cdn.example.com"


def test_s3_iam_role_mode_builds_the_aws_credential_chain(s3_backend, monkeypatch):
    """S3_USE_IAM_ROLE (the default) must not require static keys."""
    from minio.credentials import ChainedProvider

    monkeypatch.setattr(settings, "S3_USE_IAM_ROLE", True)
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "")
    monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "")

    assert isinstance(sb._iam_role_credentials(), ChainedProvider)
    assert isinstance(sb.build_storage_client()._provider, ChainedProvider)


def test_s3_rejects_single_put_above_five_gib(s3_backend):
    """AWS caps a single PUT at 5 GiB; anything larger must take the multipart path."""
    assert sb.single_put_max_bytes() == sb.S3_SINGLE_PUT_MAX_BYTES == 5 * 1024**3
    assert sb.supports_single_put(5 * 1024**3) is True
    assert sb.supports_single_put(5 * 1024**3 + 1) is False
    assert sb.supports_single_put(6 * 1024**3) is False


def test_unknown_upload_size_still_allows_a_presigned_put(s3_backend):
    """/complete re-checks the size the backend observed, so an unknown size is fine."""
    assert sb.supports_single_put(None) is True


# ---------------------------------------------------------------------------
# Presigned-URL TTL clamp (A1.12)
# ---------------------------------------------------------------------------


def test_clamp_caps_the_legacy_twenty_four_hour_ttl():
    assert settings.PRESIGNED_URL_MAX_SECONDS == 21600  # 6 h
    assert sb.clamp_presigned_expiry(86400) == 21600


def test_clamp_leaves_shorter_lifetimes_alone():
    assert sb.clamp_presigned_expiry(3600) == 3600
    assert sb.clamp_presigned_expiry(21600) == 21600


def test_clamp_treats_non_positive_and_invalid_input_as_the_ceiling():
    assert sb.clamp_presigned_expiry(0) == 21600
    assert sb.clamp_presigned_expiry(-1) == 21600
    assert sb.clamp_presigned_expiry(None) == 21600
    assert sb.clamp_presigned_expiry("nonsense") == 21600  # type: ignore[arg-type]


def test_clamp_enforces_a_usable_floor():
    assert sb.clamp_presigned_expiry(5) == sb.MIN_PRESIGNED_SECONDS


def test_clamp_ceiling_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "PRESIGNED_URL_MAX_SECONDS", 900)
    assert sb.clamp_presigned_expiry(3600) == 900


# ---------------------------------------------------------------------------
# Bucket CORS (A1.11)
# ---------------------------------------------------------------------------


def test_cors_is_a_no_op_on_minio(minio_backend, monkeypatch):
    """MinIO already answers every origin; we must never rewrite its config."""
    monkeypatch.setattr(settings, "S3_CONFIGURE_BUCKET_CORS", True)
    with patch("boto3.client") as boto_client:
        assert sb.ensure_bucket_cors("opentranscribe") is False
    boto_client.assert_not_called()


def test_cors_is_off_by_default_on_s3(s3_backend, monkeypatch):
    """Overwriting a bucket's CORS configuration is destructive — opt-in only."""
    monkeypatch.setattr(settings, "S3_CONFIGURE_BUCKET_CORS", False)
    with patch("boto3.client") as boto_client:
        assert sb.ensure_bucket_cors("ot-media") is False
    boto_client.assert_not_called()


def test_cors_rule_allows_browser_put_and_exposes_etag(s3_backend, monkeypatch):
    monkeypatch.setattr(settings, "S3_CONFIGURE_BUCKET_CORS", True)
    monkeypatch.setattr(
        settings, "S3_CORS_ALLOWED_ORIGINS", "https://app.example.com, https://alt.example.com"
    )
    fake = MagicMock()
    with patch("boto3.client", return_value=fake) as boto_client:
        assert sb.ensure_bucket_cors("ot-media") is True

    boto_client.assert_called_once()
    assert boto_client.call_args.kwargs["region_name"] == "eu-west-2"
    assert boto_client.call_args.kwargs["endpoint_url"] == "https://s3.eu-west-2.amazonaws.com"

    rule = fake.put_bucket_cors.call_args.kwargs["CORSConfiguration"]["CORSRules"][0]
    assert fake.put_bucket_cors.call_args.kwargs["Bucket"] == "ot-media"
    assert "PUT" in rule["AllowedMethods"]
    assert rule["AllowedOrigins"] == ["https://app.example.com", "https://alt.example.com"]
    # ETag must be readable cross-origin or multipart completion cannot be driven.
    assert "ETag" in rule["ExposeHeaders"]


def test_cors_falls_back_to_the_app_cors_origins(s3_backend, monkeypatch):
    monkeypatch.setattr(settings, "S3_CONFIGURE_BUCKET_CORS", True)
    monkeypatch.setattr(settings, "S3_CORS_ALLOWED_ORIGINS", "")
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://only.example.com"])
    assert sb.cors_allowed_origins() == ["https://only.example.com"]


def test_cors_failure_never_raises(s3_backend, monkeypatch):
    """Bucket CORS runs on the startup path — a failure must not block boot."""
    monkeypatch.setattr(settings, "S3_CONFIGURE_BUCKET_CORS", True)
    monkeypatch.setattr(settings, "S3_CORS_ALLOWED_ORIGINS", "https://app.example.com")
    fake = MagicMock()
    fake.put_bucket_cors.side_effect = RuntimeError("AccessDenied")
    with patch("boto3.client", return_value=fake):
        assert sb.ensure_bucket_cors("ot-media") is False


# ---------------------------------------------------------------------------
# Module-level client construction (the import-time singleton)
# ---------------------------------------------------------------------------


def test_minio_service_client_honours_storage_backend_at_import(run_in_clean_process):
    """``minio_service.minio_client`` is built once at import — prove the env reaches it."""
    code = (
        "from app.services.minio_service import minio_client\n"
        "b = minio_client._base_url\n"
        "print(f'{b._url.geturl()}|{b._virtual_style_flag}|{b.region}')\n"
    )
    assert (
        run_in_clean_process(
            code,
            TESTING="true",
            STORAGE_BACKEND="s3",
            S3_REGION="us-west-2",
            S3_USE_IAM_ROLE="false",
            AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE",  # gitleaks:allow
            AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # gitleaks:allow
        )
        == "https://s3.us-west-2.amazonaws.com|True|us-west-2"
    )
