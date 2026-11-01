"""Revocation answers must be about THIS certificate and current, against real signed data.

OCSP is fetched over plain HTTP and a CRL over the network, so a correctly signed answer
is not enough: an OCSP GOOD issued for another serial or another issuer, or one that is
stale or not yet valid, must count as no answer (inconclusive -> CRL fallback ->
``pki_revocation_soft_fail``). The same holds for a CRL past its ``next_update`` or not yet
valid. An empty-but-valid CRL is a real answer and is cached like any other.
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.x509 import ocsp

from app.auth import pki_ocsp
from app.auth import pki_revocation
from app.core.process_auth_settings import publish_process_auth_setting
from tests.fixtures.pki_chain import crl
from tests.fixtures.pki_chain import issue
from tests.fixtures.pki_chain import make_ca
from tests.fixtures.pki_chain import make_chain
from tests.fixtures.pki_chain import ocsp_response

GOOD = ocsp.OCSPCertStatus.GOOD
REVOKED = ocsp.OCSPCertStatus.REVOKED

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_state():
    publish_process_auth_setting("pki_crl_cache_seconds", 3600)
    publish_process_auth_setting("pki_ocsp_timeout_seconds", 5)
    pki_ocsp._ocsp_cache.clear()
    pki_revocation._crl_cache.clear()
    yield
    pki_ocsp._ocsp_cache.clear()
    pki_revocation._crl_cache.clear()


@pytest.fixture(scope="module")
def chain():
    return make_chain()


def _ocsp_verdict(chain, der: bytes) -> bool | None:
    with patch("app.auth.pki_ocsp._send_ocsp_request", return_value=der):
        return pki_ocsp._check_ocsp(chain.leaf_cert, chain.ca_cert)


class TestOcspBinding:
    def test_bound_current_good_is_valid(self, chain):
        assert _ocsp_verdict(chain, ocsp_response(chain, GOOD)) is False

    def test_good_for_another_serial_is_inconclusive(self, chain):
        other, _ = issue(chain.ca_cert, chain.ca_key, "Someone Else")
        der = ocsp_response(chain, GOOD, subject=other)
        assert _ocsp_verdict(chain, der) is None
        assert pki_ocsp._get_ocsp_cache(str(chain.leaf_cert.serial_number)) is None

    def test_good_for_the_same_serial_under_another_issuer_is_inconclusive(self, chain):
        other_ca, _ = make_ca("Other CA")
        der = ocsp_response(chain, GOOD, subject_issuer=other_ca)
        assert _ocsp_verdict(chain, der) is None

    def test_revoked_for_another_serial_is_not_a_verdict_about_this_one(self, chain):
        other, _ = issue(chain.ca_cert, chain.ca_key, "Someone Else")
        assert _ocsp_verdict(chain, ocsp_response(chain, REVOKED, subject=other)) is None


class TestOcspFreshness:
    def test_expired_good_is_inconclusive(self, chain):
        der = ocsp_response(chain, GOOD, age=timedelta(hours=3), next_update=timedelta(hours=1))
        assert _ocsp_verdict(chain, der) is None

    def test_not_yet_valid_good_is_inconclusive(self, chain):
        assert _ocsp_verdict(chain, ocsp_response(chain, GOOD, age=-timedelta(hours=1))) is None

    def test_small_clock_skew_is_tolerated(self, chain):
        der = ocsp_response(chain, GOOD, age=-timedelta(seconds=30))
        assert _ocsp_verdict(chain, der) is False

    def test_good_without_next_update_within_max_age_is_valid(self, chain):
        assert _ocsp_verdict(chain, ocsp_response(chain, GOOD, next_update=None)) is False

    def test_good_without_next_update_past_max_age_is_inconclusive(self, chain):
        der = ocsp_response(chain, GOOD, next_update=None, age=timedelta(days=2))
        assert _ocsp_verdict(chain, der) is None

    def test_good_is_never_cached_past_its_next_update(self, chain):
        der = ocsp_response(chain, GOOD, age=timedelta(minutes=50), next_update=timedelta(hours=1))
        assert _ocsp_verdict(chain, der) is False
        _, expiry = pki_ocsp._ocsp_cache[str(chain.leaf_cert.serial_number)]
        assert expiry - time.time() <= 11 * 60


class TestCrlFreshness:
    def _check(self, chain, der: bytes) -> bool | None:
        with patch("app.auth.pki_revocation._download_crl", return_value=der):
            return pki_revocation._check_crl(chain.leaf_cert, chain.ca_cert)

    def test_current_crl_is_a_verdict(self, chain):
        assert self._check(chain, crl(chain.ca_cert, chain.ca_key)) is False

    def test_crl_past_next_update_is_refused(self, chain):
        stale = crl(
            chain.ca_cert,
            chain.ca_key,
            last_update=-timedelta(days=3),
            next_update=-timedelta(days=1),
        )
        assert self._check(chain, stale) is None

    def test_crl_not_yet_valid_is_refused(self, chain):
        future = crl(chain.ca_cert, chain.ca_key, last_update=timedelta(hours=2))
        assert self._check(chain, future) is None

    def test_cached_crl_that_went_stale_is_not_used(self, chain):
        url = "http://crl-a.pki-chain.test/ca.crl"
        # Non-empty, so a truthiness check on the cache hit cannot hide the staleness.
        stale = crl(
            chain.ca_cert,
            chain.ca_key,
            (12345,),
            last_update=-timedelta(days=3),
            next_update=-timedelta(days=1),
        )
        pki_revocation._set_crl_cache(url, x509.load_der_x509_crl(stale))
        with patch("app.auth.pki_revocation._download_crl", return_value=None):
            assert pki_revocation._get_or_download_crl(url, chain.ca_cert) is None


class TestEmptyCrlIsCached:
    def test_empty_valid_crl_is_served_from_cache(self, chain):
        empty = crl(chain.ca_cert, chain.ca_key)
        url = "http://crl-a.pki-chain.test/ca.crl"
        with patch("app.auth.pki_revocation._download_crl", return_value=empty) as dl:
            assert pki_revocation._get_or_download_crl(url, chain.ca_cert) is not None
            assert pki_revocation._get_or_download_crl(url, chain.ca_cert) is not None
        assert dl.call_count == 1
