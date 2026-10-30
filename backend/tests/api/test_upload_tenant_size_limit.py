"""The per-tenant upload size ceiling is enforced on the legacy multipart route.

``POST /api/files`` -> ``process_file_upload`` must call ``validate_file_size_for_tenant``
once the true byte count is known. The unit tests cover the validator and the streaming
tests cover the global body cap, but nothing drove a tier ceiling through this route, so
deleting the call turned no test red.
"""

from __future__ import annotations

import io

import pytest
from fastapi import status

from app.core.tenant_limits import TenantUploadLimits
from app.core.tenant_limits import reset_resolvers
from app.core.tenant_limits import set_upload_limits_resolver


@pytest.fixture(autouse=True)
def _restore_resolvers():
    reset_resolvers()
    yield
    reset_resolvers()


def _post(client, headers, wav: bytes):
    return client.post(
        "/api/files",
        headers=headers,
        files={"file": ("tier.wav", io.BytesIO(wav), "audio/wav")},
    )


def test_payload_over_the_tier_ceiling_is_413_with_plan_message(
    client, user_token_headers, sample_wav_bytes
):
    ceiling = len(sample_wav_bytes) - 1
    set_upload_limits_resolver(lambda _org: TenantUploadLimits(max_file_bytes=ceiling))

    response = _post(client, user_token_headers, sample_wav_bytes)

    assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, response.text
    assert response.json()["detail"] == (
        f"File exceeds your plan's maximum upload size of "
        f"{ceiling / (1024**3):.1f} GB. Upgrade your plan to upload larger files."
    )


def test_payload_under_the_tier_ceiling_uploads(
    client, user_token_headers, sample_wav_bytes, monkeypatch
):
    # Storage and pipeline dispatch are outside what this test pins; stubbing them
    # keeps the positive control runnable without MinIO/Celery.
    stored: list[int] = []
    monkeypatch.setattr(
        "app.api.endpoints.files.upload.upload_file_to_storage",
        lambda content, size, path, ctype: stored.append(size),
    )
    monkeypatch.setattr(
        "app.api.endpoints.files.upload.dispatch_upload_pipeline_or_mark_error",
        lambda *a, **k: None,
    )
    set_upload_limits_resolver(
        lambda _org: TenantUploadLimits(max_file_bytes=len(sample_wav_bytes) + 1)
    )

    response = _post(client, user_token_headers, sample_wav_bytes)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert stored == [len(sample_wav_bytes)]
