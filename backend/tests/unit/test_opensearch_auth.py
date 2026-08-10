"""OpenSearch connection auth + embedding mode (issue #284 A1.13).

``OPENSEARCH_AUTH=basic`` is the default and must reproduce the exact kwargs the five
former inline ``OpenSearch(...)`` literals used. ``sigv4`` is what an Amazon OpenSearch
Service domain with an IAM access policy requires; there is no such domain here, so the
signer is built from a stubbed botocore credential object and only the resulting client
configuration is asserted — **not** that a real domain accepts it.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core import opensearch_auth
from app.core.config import settings


@pytest.fixture
def basic_auth(monkeypatch):
    monkeypatch.setattr(settings, "OPENSEARCH_AUTH", "basic")
    monkeypatch.setattr(settings, "OPENSEARCH_HOST", "opensearch")
    monkeypatch.setattr(settings, "OPENSEARCH_PORT", "9200")
    monkeypatch.setattr(settings, "OPENSEARCH_USER", "admin")
    monkeypatch.setattr(settings, "OPENSEARCH_PASSWORD", "admin-pw")  # gitleaks:allow
    monkeypatch.setattr(settings, "OPENSEARCH_USE_TLS", False)
    monkeypatch.setattr(settings, "OPENSEARCH_VERIFY_CERTS", False)


@pytest.fixture
def sigv4_auth(monkeypatch):
    monkeypatch.setattr(settings, "OPENSEARCH_AUTH", "sigv4")
    monkeypatch.setattr(settings, "OPENSEARCH_HOST", "search-x.eu-west-2.es.amazonaws.com")
    monkeypatch.setattr(settings, "OPENSEARCH_PORT", "443")
    monkeypatch.setattr(settings, "OPENSEARCH_AWS_REGION", "eu-west-2")
    monkeypatch.setattr(settings, "OPENSEARCH_AWS_SERVICE", "es")
    monkeypatch.setattr(settings, "OPENSEARCH_USE_TLS", False)  # deliberately wrong


def _stub_session(credentials=object()):
    """boto3.Session() stub whose get_credentials() returns *credentials*."""
    session = MagicMock()
    session.get_credentials.return_value = credentials
    return MagicMock(return_value=session)


# ---------------------------------------------------------------------------
# basic (default)
# ---------------------------------------------------------------------------


def test_basic_is_the_default():
    assert settings.OPENSEARCH_AUTH == "basic"
    assert opensearch_auth.is_sigv4() is False


def test_basic_kwargs_match_the_previous_inline_client(basic_auth):
    kwargs = opensearch_auth.opensearch_connection_kwargs()
    assert kwargs == {
        "hosts": [{"host": "opensearch", "port": 9200}],
        "http_auth": ("admin", "admin-pw"),
        "use_ssl": False,
        "verify_certs": False,
        "ssl_show_warn": False,
    }


def test_basic_does_not_impose_a_connection_class(basic_auth):
    """Three of the five call sites never set one — keep their urllib3 default."""
    assert "connection_class" not in opensearch_auth.opensearch_connection_kwargs()


def test_overrides_are_merged(basic_auth):
    from opensearchpy import RequestsHttpConnection

    kwargs = opensearch_auth.opensearch_connection_kwargs(
        connection_class=RequestsHttpConnection, timeout=30
    )
    assert kwargs["connection_class"] is RequestsHttpConnection
    assert kwargs["timeout"] == 30


def test_basic_respects_tls_settings(basic_auth, monkeypatch):
    monkeypatch.setattr(settings, "OPENSEARCH_USE_TLS", True)
    monkeypatch.setattr(settings, "OPENSEARCH_VERIFY_CERTS", True)
    kwargs = opensearch_auth.opensearch_connection_kwargs()
    assert kwargs["use_ssl"] is True
    assert kwargs["verify_certs"] is True


# ---------------------------------------------------------------------------
# sigv4
# ---------------------------------------------------------------------------


def test_sigv4_signs_with_the_configured_region_and_service(sigv4_auth):
    from opensearchpy import AWSV4SignerAuth

    with patch("boto3.Session", _stub_session()):
        kwargs = opensearch_auth.opensearch_connection_kwargs()

    assert opensearch_auth.is_sigv4() is True
    assert opensearch_auth.signing_region() == "eu-west-2"
    assert isinstance(kwargs["http_auth"], AWSV4SignerAuth)
    assert kwargs["http_auth"].service == "es"


def test_sigv4_forces_the_requests_transport(sigv4_auth):
    """AWSV4SignerAuth is a requests AuthBase: under urllib3 it is silently ignored
    and every request goes out unsigned, which the domain answers with 403."""
    from opensearchpy import RequestsHttpConnection
    from opensearchpy import Urllib3HttpConnection

    with patch("boto3.Session", _stub_session()):
        kwargs = opensearch_auth.opensearch_connection_kwargs(
            connection_class=Urllib3HttpConnection
        )
    assert kwargs["connection_class"] is RequestsHttpConnection


def test_sigv4_forces_tls_even_when_misconfigured(sigv4_auth):
    """Signing over plaintext would put a replayable Authorization header on the wire."""
    with patch("boto3.Session", _stub_session()):
        kwargs = opensearch_auth.opensearch_connection_kwargs()
    assert kwargs["use_ssl"] is True
    assert kwargs["verify_certs"] is True


def test_sigv4_never_sends_the_basic_auth_password(sigv4_auth):
    with patch("boto3.Session", _stub_session()):
        kwargs = opensearch_auth.opensearch_connection_kwargs()
    assert not isinstance(kwargs["http_auth"], tuple)


def test_sigv4_region_falls_back_to_aws_region(sigv4_auth, monkeypatch):
    monkeypatch.setattr(settings, "OPENSEARCH_AWS_REGION", "")
    monkeypatch.setattr(settings, "AWS_REGION", "ap-southeast-2")
    assert opensearch_auth.signing_region() == "ap-southeast-2"


def test_sigv4_serverless_uses_the_aoss_signing_service(sigv4_auth, monkeypatch):
    monkeypatch.setattr(settings, "OPENSEARCH_AWS_SERVICE", "aoss")
    with patch("boto3.Session", _stub_session()):
        kwargs = opensearch_auth.opensearch_connection_kwargs()
    assert kwargs["http_auth"].service == "aoss"


def test_sigv4_without_resolvable_credentials_raises(sigv4_auth):
    """Falling back to basic auth would leak the OpenSearch password to AWS."""
    with patch("boto3.Session", _stub_session(credentials=None)):
        with pytest.raises(RuntimeError, match="no AWS credentials"):
            opensearch_auth.opensearch_connection_kwargs()


# ---------------------------------------------------------------------------
# Embedding mode
# ---------------------------------------------------------------------------


def test_local_embedding_mode_is_the_default():
    from app.main import _managed_embedding_mode

    assert settings.OPENSEARCH_EMBEDDING_MODE == "local"
    assert _managed_embedding_mode() is False


def test_managed_embedding_mode_is_detected(monkeypatch):
    from app.main import _managed_embedding_mode

    monkeypatch.setattr(settings, "OPENSEARCH_EMBEDDING_MODE", "managed")
    assert _managed_embedding_mode() is True


def test_managed_mode_adopts_the_configured_model_without_touching_the_cluster(
    monkeypatch,
):
    """A managed domain exposes neither the ML Commons cluster settings nor URL-based
    model registration, so neither may be attempted."""
    from app.main import _adopt_managed_embedding_model

    monkeypatch.setattr(settings, "OPENSEARCH_EMBEDDING_MODE", "managed")
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_MODEL_ID", "abc123")
    ml_service = MagicMock()

    with patch(
        "app.services.search.indexing_service.ensure_neural_ingest_pipeline", return_value=True
    ) as ensure_pipeline:
        _adopt_managed_embedding_model(ml_service)

    ml_service.set_active_model_id.assert_called_once_with("abc123")
    ml_service.configure_ml_settings.assert_not_called()
    ml_service.ensure_model_deployed.assert_not_called()
    ensure_pipeline.assert_called_once()


def test_managed_mode_without_a_model_id_falls_back_to_the_active_one(monkeypatch):
    from app.main import _adopt_managed_embedding_model

    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_MODEL_ID", "")
    ml_service = MagicMock()
    ml_service.get_active_model_id.return_value = "already-registered"

    with patch(
        "app.services.search.indexing_service.ensure_neural_ingest_pipeline", return_value=True
    ):
        _adopt_managed_embedding_model(ml_service)

    ml_service.set_active_model_id.assert_called_once_with("already-registered")


def test_managed_mode_with_no_model_at_all_is_a_logged_no_op(monkeypatch):
    from app.main import _adopt_managed_embedding_model

    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_MODEL_ID", "")
    ml_service = MagicMock()
    ml_service.get_active_model_id.return_value = None

    with patch(
        "app.services.search.indexing_service.ensure_neural_ingest_pipeline"
    ) as ensure_pipeline:
        _adopt_managed_embedding_model(ml_service)

    ml_service.set_active_model_id.assert_not_called()
    ensure_pipeline.assert_not_called()
