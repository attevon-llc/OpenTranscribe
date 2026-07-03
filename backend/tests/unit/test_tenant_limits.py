"""Unit tests for the per-tenant limit resolver seam (app.core.tenant_limits).

GPU-free, no DB. Verifies the community no-op default and that a registered
resolver overrides it — plus the upload-size validation helper that the upload
paths call.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_resolvers():
    from app.core.tenant_limits import reset_resolvers

    reset_resolvers()
    yield
    reset_resolvers()


def test_community_default_is_noop():
    from app.core.tenant_limits import min_retention_override_days
    from app.core.tenant_limits import resolve_retention_days
    from app.core.tenant_limits import resolve_upload_limits

    # No resolver registered -> every dimension falls back to the global value.
    assert resolve_retention_days(None) is None
    assert resolve_retention_days(123) is None
    assert resolve_upload_limits(123) is None
    assert min_retention_override_days() is None


def test_registered_retention_resolver_overrides():
    from app.core.tenant_limits import min_retention_override_days
    from app.core.tenant_limits import resolve_retention_days
    from app.core.tenant_limits import set_retention_resolver

    # A free tier with 7-day retention alongside a 365-day premium tier: the
    # cleanup candidate window must key on the SHORTEST override so free-tier
    # files become candidates early (per-file expiry keeps everyone else).
    set_retention_resolver(
        lambda org_id: {7: 365, 9: 7}.get(org_id) if org_id is not None else None,
        lambda: 7,
    )
    assert resolve_retention_days(7) == 365
    assert resolve_retention_days(9) == 7
    assert resolve_retention_days(8) is None  # no override for this org
    assert min_retention_override_days() == 7


def test_registered_upload_limits_resolver_overrides():
    from app.core.tenant_limits import TenantUploadLimits
    from app.core.tenant_limits import resolve_upload_limits
    from app.core.tenant_limits import set_upload_limits_resolver

    set_upload_limits_resolver(
        lambda org_id: (
            TenantUploadLimits(max_file_bytes=1024, max_duration_seconds=60) if org_id else None
        )
    )
    limits = resolve_upload_limits(5)
    assert limits is not None
    assert limits.max_file_bytes == 1024
    assert limits.max_duration_seconds == 60
    assert resolve_upload_limits(None) is None


def test_resolver_error_falls_back_to_global():
    from app.core.tenant_limits import resolve_retention_days
    from app.core.tenant_limits import resolve_upload_limits
    from app.core.tenant_limits import set_retention_resolver
    from app.core.tenant_limits import set_upload_limits_resolver

    def _boom(_org_id):
        raise RuntimeError("resolver exploded")

    set_retention_resolver(_boom)
    set_upload_limits_resolver(_boom)
    # A misbehaving resolver must never break the caller -> global fallback.
    assert resolve_retention_days(1) is None
    assert resolve_upload_limits(1) is None


def test_validate_file_size_for_tenant_blocks_over_limit():
    from fastapi import HTTPException

    from app.api.endpoints.files.upload import validate_file_size_for_tenant
    from app.core.tenant_limits import TenantUploadLimits
    from app.core.tenant_limits import set_upload_limits_resolver

    set_upload_limits_resolver(
        lambda org_id: TenantUploadLimits(max_file_bytes=1000) if org_id else None
    )
    # Within the limit -> no error.
    validate_file_size_for_tenant(500, organization_id=1)
    # Unknown size (0) -> never rejected here.
    validate_file_size_for_tenant(0, organization_id=1)
    # Personal (no org) -> no override -> no error.
    validate_file_size_for_tenant(10_000, organization_id=None)
    # Over the per-tenant limit -> 413.
    with pytest.raises(HTTPException) as exc:
        validate_file_size_for_tenant(2000, organization_id=1)
    assert exc.value.status_code == 413
