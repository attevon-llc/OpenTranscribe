"""OCSP response verification fails closed, against real signed responses.

OCSP is fetched over plain HTTP and a ``False`` (= not revoked) verdict short-circuits the
CRL cross-check in ``_check_revocation``, so every way a response can be unverifiable must
read as inconclusive (``None``), never as GOOD. These tests sign responses with the
configured CA, a delegated responder, an unrelated CA and unsupported key types, and pin
the verdict, the cache it leaves behind, and the transport refusals.
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from cryptography.x509 import ocsp

from app.auth import pki_ocsp
from app.core.config import settings
from app.core.process_auth_settings import publish_process_auth_setting
from tests.fixtures.pki_chain import OCSP_URL
from tests.fixtures.pki_chain import issue
from tests.fixtures.pki_chain import make_ca
from tests.fixtures.pki_chain import make_chain
from tests.fixtures.pki_chain import ocsp_response

GOOD = ocsp.OCSPCertStatus.GOOD
REVOKED = ocsp.OCSPCertStatus.REVOKED
UNKNOWN = ocsp.OCSPCertStatus.UNKNOWN

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_ocsp_state():
    publish_process_auth_setting("pki_crl_cache_seconds", 3600)
    publish_process_auth_setting("pki_ocsp_timeout_seconds", 5)
    pki_ocsp._ocsp_cache.clear()
    yield
    pki_ocsp._ocsp_cache.clear()


@pytest.fixture(scope="module")
def chain():
    return make_chain()


def _parse(chain, der: bytes) -> bool | None:
    return pki_ocsp._parse_ocsp_response(der, chain.ca_cert, str(chain.leaf_cert.serial_number))


def _cached(chain) -> bool | None:
    return pki_ocsp._get_ocsp_cache(str(chain.leaf_cert.serial_number))


class TestStatusVerdicts:
    def test_ca_signed_revoked_is_revoked_and_cached_revoked(self, chain):
        assert _parse(chain, ocsp_response(chain, REVOKED)) is True
        assert _cached(chain) is True

    def test_ca_signed_good_is_good_and_cached_good(self, chain):
        assert _parse(chain, ocsp_response(chain, GOOD)) is False
        assert _cached(chain) is False

    def test_unknown_status_is_inconclusive_and_not_cached(self, chain):
        assert _parse(chain, ocsp_response(chain, UNKNOWN)) is None
        assert _cached(chain) is None

    def test_unparseable_response_is_inconclusive(self, chain):
        assert _parse(chain, b"\x30\x03not-ocsp") is None

    def test_good_ttl_is_capped_by_crl_cache_seconds(self, chain):
        publish_process_auth_setting("pki_crl_cache_seconds", 60)
        before = time.time()
        assert _parse(chain, ocsp_response(chain, GOOD, next_update=timedelta(days=7))) is False
        _, expiry = pki_ocsp._ocsp_cache[str(chain.leaf_cert.serial_number)]
        assert expiry <= before + 61


class TestSignatureFailsClosed:
    def test_good_signed_by_an_unrelated_key_is_inconclusive(self, chain):
        # Named and signed by the rogue CA, nothing embedded: checked against the CA.
        der = ocsp_response(
            chain, GOOD, signer_key=chain.rogue_key, responder_cert=chain.rogue_cert
        )
        assert _parse(chain, der) is None
        assert _cached(chain) is None

    def test_good_from_a_responder_the_ca_did_not_issue_is_inconclusive(self, chain):
        rogue_responder, rogue_responder_key = issue(
            chain.rogue_cert, chain.rogue_key, "Rogue Responder", ocsp_signing=True
        )
        der = ocsp_response(
            chain,
            GOOD,
            signer_key=rogue_responder_key,
            responder_cert=rogue_responder,
            embed=(rogue_responder,),
        )
        assert _parse(chain, der) is None

    def test_good_from_a_delegated_responder_the_ca_issued_is_accepted(self, chain):
        responder, responder_key = issue(
            chain.ca_cert, chain.ca_key, "Delegated Responder", ocsp_signing=True
        )
        der = ocsp_response(
            chain, GOOD, signer_key=responder_key, responder_cert=responder, embed=(responder,)
        )
        assert _parse(chain, der) is False

    def test_unsupported_signature_algorithm_is_inconclusive(self):
        ed_chain = make_chain(kind="ed25519")
        assert _parse(ed_chain, ocsp_response(ed_chain, GOOD)) is None

    def test_responder_key_type_that_cannot_verify_is_inconclusive(self, chain):
        # An RSA-SHA256 response checked against a CA whose key is Ed25519: the hash is
        # known, the key type is not one this module verifies with.
        ed_ca, _ = make_ca("Ed25519 CA", kind="ed25519")
        der = ocsp_response(
            chain, GOOD, signer_key=chain.rogue_key, responder_cert=chain.rogue_cert
        )
        result = pki_ocsp._parse_ocsp_response(der, ed_ca, str(chain.leaf_cert.serial_number))
        assert result is None

    def test_ec_ca_signed_revoked_is_revoked(self):
        ec_chain = make_chain(kind="ec")
        assert _parse(ec_chain, ocsp_response(ec_chain, REVOKED)) is True

    def test_unexpected_verification_error_reads_as_unverified(self, chain):
        resp = ocsp.load_der_ocsp_response(ocsp_response(chain, GOOD))
        broken_issuer = MagicMock()
        broken_issuer.public_key.side_effect = RuntimeError("HSM unavailable")
        assert pki_ocsp._verify_ocsp_signature(resp, broken_issuer, "1") is False

    def test_wrong_key_signature_reads_as_unverified(self, chain):
        der = ocsp_response(
            chain, GOOD, signer_key=chain.rogue_key, responder_cert=chain.rogue_cert
        )
        resp = ocsp.load_der_ocsp_response(der)
        assert pki_ocsp._verify_ocsp_signature(resp, chain.ca_cert, "1") is False


class TestCheckOcsp:
    def test_cached_verdict_is_served_without_a_request(self, chain):
        serial = str(chain.leaf_cert.serial_number)
        pki_ocsp._set_ocsp_cache(serial, True, 300)
        with patch("app.auth.pki_ocsp._send_ocsp_request", return_value=None) as send:
            assert pki_ocsp._check_ocsp(chain.leaf_cert, chain.ca_cert) is True
        assert not send.called

    def test_fetched_revoked_response_is_revoked(self, chain):
        der = ocsp_response(chain, REVOKED)
        with patch("app.auth.pki_ocsp._send_ocsp_request", return_value=der) as send:
            assert pki_ocsp._check_ocsp(chain.leaf_cert, chain.ca_cert) is True
        assert send.call_args.args[0] == OCSP_URL

    def test_expired_cache_entry_is_not_served(self, chain):
        serial = str(chain.leaf_cert.serial_number)
        pki_ocsp._ocsp_cache[serial] = (False, time.time() - 1)
        assert pki_ocsp._get_ocsp_cache(serial) is None
        assert serial not in pki_ocsp._ocsp_cache


class TestTransportAndCache:
    def test_non_200_responder_reply_is_no_response(self):
        reply = MagicMock(status_code=500, content=b"error page")
        with patch("app.auth.pki_ocsp.httpx.Client") as client:
            client.return_value.__enter__.return_value.post.return_value = reply
            assert pki_ocsp._send_ocsp_request("http://ocsp.example.test", b"req") is None

    def test_200_reply_returns_its_body(self):
        reply = MagicMock(status_code=200, content=b"der-bytes")
        with patch("app.auth.pki_ocsp.httpx.Client") as client:
            client.return_value.__enter__.return_value.post.return_value = reply
            assert pki_ocsp._send_ocsp_request("http://ocsp.example.test", b"req") == b"der-bytes"

    def test_cache_is_bounded_lru(self, monkeypatch):
        monkeypatch.setattr(settings, "PKI_OCSP_CACHE_MAX_SIZE", 2)
        for serial in ("a", "b", "c"):
            pki_ocsp._set_ocsp_cache(serial, False, 300)
        assert list(pki_ocsp._ocsp_cache) == ["b", "c"]
