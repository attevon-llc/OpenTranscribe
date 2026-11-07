"""The shared storage client must keep minio-py's transport policy with a bigger pool (#1378).

minio-py replaces its whole default ``PoolManager`` (retries, timeouts, CA handling) when an
``http_client`` is passed, and its default pool holds only 10 connections per host. These tests
pin what the shared client must have: a pool sized to the process concurrency, minio's retry
policy, and the same CA handling.
"""

from __future__ import annotations

import ssl

import certifi
import pytest
from minio import Minio
from urllib3 import Retry

from app.core.threadpool import api_threadpool_size
from app.services import minio_service

pytestmark = pytest.mark.unit


def _shared_client() -> Minio:
    """A client built exactly as minio_service builds the module-level shared one."""
    from app.services import storage_backend

    return storage_backend.build_storage_client(http_client=minio_service.shared_http_pool())


def test_module_level_shared_client_uses_the_sized_pool():
    kw = minio_service.minio_client._http.connection_pool_kw
    assert kw["maxsize"] == max(10, api_threadpool_size())
    assert kw["retries"].status_forcelist == [500, 502, 503, 504]


def test_pool_is_sized_to_the_process_concurrency():
    kw = _shared_client()._http.connection_pool_kw
    assert kw["maxsize"] == max(10, api_threadpool_size())
    assert kw["maxsize"] > 10, "the point of #1378: minio-py's default of 10 starves 40 threads"


def test_retry_policy_is_minios_default():
    retries = _shared_client()._http.connection_pool_kw["retries"]
    assert isinstance(retries, Retry)
    assert retries.total == 5
    assert retries.backoff_factor == 0.2
    assert set(retries.status_forcelist) == {500, 502, 503, 504}


def test_timeouts_match_minios_five_minutes():
    timeout = _shared_client()._http.connection_pool_kw["timeout"]
    assert timeout.connect_timeout == 300
    assert timeout.read_timeout == 300


def test_tls_is_verified_against_certifi_by_default(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    kw = _shared_client()._http.connection_pool_kw
    assert kw["cert_reqs"] == "CERT_REQUIRED" or kw["cert_reqs"] == ssl.CERT_REQUIRED
    assert kw["ca_certs"] == certifi.where()


def test_ssl_cert_file_overrides_the_ca_bundle(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/custom-ca.pem")
    assert _shared_client()._http.connection_pool_kw["ca_certs"] == "/etc/ssl/custom-ca.pem"


def test_an_explicit_http_client_is_still_honoured():
    """The probe and takedown clients pass their own bounded pool; it must not be replaced."""
    import urllib3

    from app.services import storage_backend as sb

    pool = urllib3.PoolManager(retries=urllib3.Retry(total=0))
    assert sb.build_storage_client(http_client=pool)._http is pool


def test_probe_and_takedown_clients_share_the_ca_rule(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/custom-ca.pem")
    for factory in (minio_service.get_probe_client, minio_service.get_takedown_client):
        factory.cache_clear()
        try:
            pool_kw = factory()._http.connection_pool_kw
            assert pool_kw["ca_certs"] == "/etc/ssl/custom-ca.pem"
        finally:
            factory.cache_clear()
