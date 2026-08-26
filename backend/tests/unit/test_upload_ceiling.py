"""Server-side upload ceiling (#284 A0.12, first half).

Before this there was NO server-side maximum upload size in community.
`validate_file_size_for_tenant` only enforced a limit when `resolve_upload_limits`
returned one, and `_community_upload_limits_resolver` returns None unconditionally — so
the function was a complete no-op and the only 15 GB limit in the product lived in the
browser. A client could skip the UI and PUT an arbitrarily large object to the presigned
URL, bounded by nothing.

NOT covered here: per-user storage/GPU quotas, which are the other half of A0.12.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.endpoints.files.upload import validate_file_size_for_tenant
from tests.helpers import does_not_raise

GB = 1024**3


def test_default_ceiling_matches_the_advertised_limit():
    from app.core.config import settings

    assert settings.MAX_UPLOAD_BYTES == 15 * GB


def test_file_under_the_ceiling_is_accepted():
    with does_not_raise("5 GB is well under the 15 GB ceiling"):
        validate_file_size_for_tenant(5 * GB, None)


def test_file_exactly_at_the_ceiling_is_accepted():
    """The boundary is inclusive — 15 GB is the advertised limit, not one byte over it."""
    with does_not_raise("15 GB is exactly the advertised ceiling, so it must be accepted"):
        validate_file_size_for_tenant(15 * GB, None)


def test_file_over_the_ceiling_is_rejected():
    """The hole: this used to be accepted because community had no ceiling at all."""
    with pytest.raises(HTTPException) as exc:
        validate_file_size_for_tenant(16 * GB, None)

    assert exc.value.status_code == 413


def test_wildly_oversized_upload_is_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_file_size_for_tenant(500 * GB, None)
    assert exc.value.status_code == 413


@pytest.mark.parametrize("size", [0, -1, None])
def test_unknown_size_is_not_rejected_here(size):
    """0/unknown is re-checked at complete against the size MinIO observed."""
    with does_not_raise(f"size={size!r} is unknown, not oversized, so this gate must pass it"):
        validate_file_size_for_tenant(size, None)  # type: ignore[arg-type]


def test_tenant_ceiling_tightens_the_global_one(monkeypatch):
    """A per-tier limit may lower the ceiling."""
    from app.core import tenant_limits

    class _Limits:
        max_file_bytes = 2 * GB

    monkeypatch.setattr(tenant_limits, "resolve_upload_limits", lambda org: _Limits())

    with pytest.raises(HTTPException) as exc:
        validate_file_size_for_tenant(5 * GB, 1)
    assert exc.value.status_code == 413
    assert "plan" in str(exc.value.detail).lower()


def test_tenant_ceiling_cannot_loosen_the_global_one(monkeypatch):
    """A per-tier limit must never raise the ceiling above the global one."""
    from app.core import tenant_limits

    class _Limits:
        max_file_bytes = 100 * GB

    monkeypatch.setattr(tenant_limits, "resolve_upload_limits", lambda org: _Limits())

    with pytest.raises(HTTPException) as exc:
        validate_file_size_for_tenant(20 * GB, 1)
    assert exc.value.status_code == 413


def test_ceiling_can_be_disabled(monkeypatch):
    """0 disables it — only sensible on a trusted single-user install."""
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "MAX_UPLOAD_BYTES", None)
    with does_not_raise("MAX_UPLOAD_BYTES=None disables the ceiling entirely"):
        validate_file_size_for_tenant(500 * GB, None)


def test_complete_upload_enforces_against_the_observed_size():
    """The declared size only gates minting the URL; MinIO's number is authoritative."""
    import inspect

    from app.api.endpoints.files import complete_upload as mod

    source = inspect.getsource(mod.complete_upload)
    validate_at = source.index("validate_file_size_for_tenant(minio_size")
    # The call, not the import line at the top of the function.
    dispatch_at = source.index("dispatch_upload_pipeline(")

    assert validate_at < dispatch_at, "the ceiling must be enforced before dispatching to the GPU"


def test_rejected_upload_cleans_up_object_and_row():
    """An oversized object must not be left behind in MinIO or the gallery."""
    import inspect

    from app.api.endpoints.files import complete_upload as mod

    source = inspect.getsource(mod.complete_upload)
    reject_block = source[source.index("validate_file_size_for_tenant(minio_size") :]
    reject_block = reject_block[: reject_block.index("# Magic-byte validation")]

    assert "delete_file(" in reject_block
    assert "db.delete(db_file)" in reject_block
