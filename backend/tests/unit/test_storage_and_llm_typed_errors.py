"""MinIOService and LLMService raise typed errors a caller can narrow to.

Each realistic client exception maps to ``StorageError`` / ``StorageUnavailableError`` /
``LLMUnavailableError`` / ``LLMResponseError`` with the original on ``__cause__``, and a
programming error (``TypeError``) is not converted, so a caller that narrows to the typed
error does not hide a bug.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests
import urllib3
from minio.error import S3Error
from minio.error import ServerError

from app.core.exceptions import LLMResponseError
from app.core.exceptions import LLMServiceError
from app.core.exceptions import LLMUnavailableError
from app.core.exceptions import StorageError
from app.core.exceptions import StorageUnavailableError
from app.services import minio_service
from app.services.llm_service import LLMService
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider
from app.services.llm_usage import CallUsage
from app.services.minio_bucket_admin import MinIOService

_POOL = urllib3.HTTPConnectionPool("minio", 9000)


def _s3_error(code: str) -> S3Error:
    return S3Error(MagicMock(spec=urllib3.BaseHTTPResponse), code, code, "res", "req", "host")


def _client_raising(exc: BaseException) -> MagicMock:
    client = MagicMock()
    for name in ("get_object", "put_object", "remove_object", "stat_object", "bucket_exists"):
        getattr(client, name).side_effect = exc
    return client


@pytest.fixture
def wrapper(monkeypatch):
    def _make(exc: BaseException) -> MinIOService:
        monkeypatch.setattr(minio_service, "minio_client", _client_raising(exc))
        return MinIOService()

    return _make


def test_s3_error_response_is_a_storage_error_not_an_outage(wrapper):
    svc = wrapper(_s3_error("AccessDenied"))
    with pytest.raises(StorageError) as caught:
        svc.stat_object("b", "o")
    assert not isinstance(caught.value, StorageUnavailableError)
    assert isinstance(caught.value.__cause__, S3Error)
    assert "AccessDenied" in str(caught.value)
    assert "host" not in str(caught.value)


@pytest.mark.parametrize(
    "exc",
    [
        urllib3.exceptions.MaxRetryError(_POOL, "http://minio:9000/b/o"),
        ServerError("boom", 503),
        urllib3.exceptions.ProtocolError("reset"),
    ],
)
def test_no_answer_is_storage_unavailable(wrapper, exc):
    svc = wrapper(exc)
    with pytest.raises(StorageUnavailableError) as caught:
        svc.get_object("b", "o")
    assert caught.value.__cause__ is exc
    assert "minio:9000" not in str(caught.value)


def test_programming_error_is_not_converted(wrapper):
    svc = wrapper(TypeError("bug"))
    with pytest.raises(TypeError):
        svc.stat_object("b", "o")


def test_module_delete_file_maps_client_failure(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise urllib3.exceptions.MaxRetryError(_POOL, "http://minio:9000/x")

    monkeypatch.setattr("app.services.storage_versions.remove_completely", refuse)
    with pytest.raises(StorageUnavailableError):
        minio_service.delete_file("obj")


def test_module_delete_file_lets_a_bug_propagate(monkeypatch):
    def bug(*_args, **_kwargs):
        raise TypeError("bug")

    monkeypatch.setattr("app.services.storage_versions.remove_completely", bug)
    with pytest.raises(TypeError):
        minio_service.delete_file("obj")


def test_module_download_file_keeps_missing_object_distinct(monkeypatch):
    monkeypatch.setattr(minio_service, "minio_client", _client_raising(_s3_error("NoSuchKey")))
    with pytest.raises(FileNotFoundError):
        minio_service.download_file("obj")


def test_module_download_file_outage_is_typed(monkeypatch):
    monkeypatch.setattr(minio_service, "minio_client", _client_raising(ConnectionResetError("x")))
    with pytest.raises(StorageUnavailableError):
        minio_service.download_file("obj")


def test_get_file_url_outage_is_typed(monkeypatch):
    presigner = MagicMock()
    presigner.presigned_get_object.side_effect = urllib3.exceptions.MaxRetryError(_POOL, "u")
    presigner.get_presigned_url.side_effect = urllib3.exceptions.MaxRetryError(_POOL, "u")
    monkeypatch.setattr(minio_service.storage_presign_identity, "presign_client", lambda: presigner)
    with pytest.raises(StorageUnavailableError):
        minio_service.get_file_url("obj", expires=60)


def test_get_file_url_does_not_hide_a_bug(monkeypatch):
    presigner = MagicMock()
    presigner.presigned_get_object.side_effect = KeyError("bug")
    monkeypatch.setattr(minio_service.storage_presign_identity, "presign_client", lambda: presigner)
    with pytest.raises(KeyError):
        minio_service.get_file_url("obj", expires=60)


# --------------------------------------------------------------------------- LLM


@pytest.fixture
def llm(monkeypatch):
    service = LLMService(
        LLMConfig(
            provider=LLMProvider.VLLM,
            model="m",
            base_url="http://llm-test-vllm:8000/v1",
            api_key="not-a-secret",
        )
    )
    monkeypatch.setattr(service, "_resolve_endpoint", lambda: "http://llm-test-vllm:8000/v1")
    return service


def _complete(service: LLMService):
    return service._chat_completion_untracked(
        [{"role": "user", "content": "SECRET TEXT"}], CallUsage()
    )


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectionError("refused"),
        requests.exceptions.Timeout("slow"),
        requests.exceptions.RequestException("odd"),
    ],
)
def test_unreachable_provider_is_llm_unavailable(llm, monkeypatch, exc):
    def fail(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(llm, "_send_llm_request", fail)
    with pytest.raises(LLMUnavailableError) as caught:
        _complete(llm)
    assert caught.value.__cause__ is exc
    assert isinstance(caught.value, LLMServiceError)


def test_non_200_is_a_response_error_without_the_body(llm, monkeypatch):
    response = MagicMock(status_code=500, text="echo: SECRET TEXT")
    session = MagicMock()
    session.post.return_value = response
    target = MagicMock(url="http://x", headers={})
    monkeypatch.setattr(llm, "_endpoint_session", lambda _url: (session, target))
    with pytest.raises(LLMResponseError) as caught:
        llm._send_llm_request("http://x", {}, {}, 5)
    assert "500" in str(caught.value)
    assert "SECRET TEXT" not in str(caught.value)


def test_empty_content_is_a_response_error(llm, monkeypatch):
    monkeypatch.setattr(llm, "_send_llm_request", lambda *_a, **_k: {})
    monkeypatch.setattr(llm, "_extract_response_content", lambda _d: ("", 0, "stop"))
    with pytest.raises(LLMResponseError):
        _complete(llm)


def test_programming_error_in_the_llm_path_propagates(llm, monkeypatch):
    def bug(*_args, **_kwargs):
        raise TypeError("bug")

    monkeypatch.setattr(llm, "_send_llm_request", bug)
    with pytest.raises(TypeError):
        _complete(llm)
