"""OCSP revocation checking for PKI client certificates.

Builds and sends the OCSP request for a certificate, verifies the response signature
(issuer CA or a delegated responder it issued; every unverifiable case fails closed),
and caches the verdict per serial number. ``_check_ocsp`` is the entry point, called by
``auth/pki_revocation.py``'s ``_check_revocation`` before the CRL fallback.
"""

import logging
import threading
import time
from collections import OrderedDict
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.x509 import ocsp
from cryptography.x509.oid import AuthorityInformationAccessOID
from cryptography.x509.oid import ExtensionOID

from app.core.config import settings
from app.core.process_auth_settings import get_process_auth_settings

logger = logging.getLogger(__name__)

_OCSP_CLOCK_SKEW = timedelta(minutes=5)
_OCSP_MAX_AGE_WITHOUT_NEXT_UPDATE = timedelta(hours=1)


# ===== Revocation Checking Cache =====
# Thread-safe LRU cache for OCSP responses and CRLs with configurable max size

_cache_lock = threading.Lock()
# Using OrderedDict for LRU behavior - most recently used items are moved to end
_ocsp_cache: OrderedDict[str, tuple[bool, float]] = (
    OrderedDict()
)  # serial_number -> (is_revoked, expiry_time)


def _evict_lru_ocsp_cache() -> None:
    """Evict least recently used entries from OCSP cache if over max size.

    Must be called with _cache_lock held.
    """
    max_size = settings.PKI_OCSP_CACHE_MAX_SIZE
    while len(_ocsp_cache) > max_size:
        # Pop the first (oldest) item
        evicted_key, _ = _ocsp_cache.popitem(last=False)
        logger.debug(f"OCSP cache LRU eviction: removed entry for serial {evicted_key}")


def _get_ocsp_cache(serial_number: str) -> bool | None:
    """
    Get cached OCSP result for a certificate.

    Uses LRU eviction - accessed entries are moved to end of cache.

    Args:
        serial_number: Certificate serial number as string

    Returns:
        True if revoked, False if valid, None if not cached or expired
    """
    with _cache_lock:
        if serial_number in _ocsp_cache:
            is_revoked, expiry_time = _ocsp_cache[serial_number]
            if time.time() < expiry_time:
                # Move to end (most recently used) for LRU behavior
                _ocsp_cache.move_to_end(serial_number)
                return is_revoked
            # Expired, remove from cache
            del _ocsp_cache[serial_number]
    return None


def _set_ocsp_cache(serial_number: str, is_revoked: bool, ttl_seconds: int) -> None:
    """
    Cache OCSP result for a certificate.

    Uses LRU eviction when cache exceeds PKI_OCSP_CACHE_MAX_SIZE.

    Args:
        serial_number: Certificate serial number as string
        is_revoked: Whether the certificate is revoked
        ttl_seconds: Time to live in seconds
    """
    with _cache_lock:
        # If key already exists, move to end
        if serial_number in _ocsp_cache:
            _ocsp_cache.move_to_end(serial_number)
        _ocsp_cache[serial_number] = (is_revoked, time.time() + ttl_seconds)
        # Evict oldest entries if over max size
        _evict_lru_ocsp_cache()


def _get_ocsp_responder_cert(
    ocsp_response: ocsp.OCSPResponse,
    issuer_cert: x509.Certificate,
    serial_str: str,
) -> x509.Certificate | None:
    """
    Get and validate the OCSP responder certificate.

    Args:
        ocsp_response: The parsed OCSP response
        issuer_cert: The issuer (CA) certificate
        serial_str: Certificate serial number (for logging)

    Returns:
        The certificate to use for verification, or None if validation fails
    """
    responder_certs = ocsp_response.certificates

    if not responder_certs:
        # No responder certificate included - assume CA signed directly
        return issuer_cert

    # Use the first certificate as the responder certificate
    responder_cert = responder_certs[0]

    # Verify the responder certificate was issued by the CA
    try:
        responder_cert.verify_directly_issued_by(issuer_cert)
        logger.debug(f"OCSP responder certificate verified as issued by CA for serial {serial_str}")
        return responder_cert
    except Exception as e:
        logger.warning(f"OCSP responder certificate not issued by CA for serial {serial_str}: {e}")
        return None


def _get_hash_algorithm_for_sig_oid(sig_alg_oid) -> hashes.HashAlgorithm | None:
    """
    Get the hash algorithm for a signature algorithm OID.

    Args:
        sig_alg_oid: The signature algorithm OID

    Returns:
        The hash algorithm, or None if unknown
    """
    from cryptography.x509.oid import SignatureAlgorithmOID

    # Map signature algorithm OIDs to hash algorithms
    sig_oid_to_hash = {
        SignatureAlgorithmOID.RSA_WITH_SHA256: hashes.SHA256(),
        SignatureAlgorithmOID.ECDSA_WITH_SHA256: hashes.SHA256(),
        SignatureAlgorithmOID.RSA_WITH_SHA384: hashes.SHA384(),
        SignatureAlgorithmOID.ECDSA_WITH_SHA384: hashes.SHA384(),
        SignatureAlgorithmOID.RSA_WITH_SHA512: hashes.SHA512(),
        SignatureAlgorithmOID.ECDSA_WITH_SHA512: hashes.SHA512(),
        SignatureAlgorithmOID.RSA_WITH_SHA1: hashes.SHA1(),  # noqa: S303 # nosec B303 - required for legacy OCSP
    }

    return sig_oid_to_hash.get(sig_alg_oid)


def _verify_signature_by_key_type(
    public_key,
    signature: bytes,
    tbs_data: bytes,
    hash_algorithm: hashes.HashAlgorithm,
    serial_str: str,
) -> bool:
    """
    Verify a signature using the appropriate method for the key type.

    Args:
        public_key: The public key to verify with
        signature: The signature bytes
        tbs_data: The to-be-signed data
        hash_algorithm: The hash algorithm to use
        serial_str: Certificate serial number (for logging)

    Returns:
        True if signature is valid, False otherwise
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric import rsa

    if isinstance(public_key, rsa.RSAPublicKey):
        public_key.verify(
            signature,
            tbs_data,
            padding.PKCS1v15(),
            hash_algorithm,
        )
        return True

    if isinstance(public_key, ec.EllipticCurvePublicKey):
        public_key.verify(
            signature,
            tbs_data,
            ec.ECDSA(hash_algorithm),
        )
        return True

    # FAIL CLOSED. A key type we cannot verify with is not a verified signature.
    # Returning True here declared an unauthenticated OCSP response authentic;
    # the caller then accepted its GOOD status as proof of non-revocation.
    # The supported place to accept an inconclusive revocation check is
    # PKI_REVOCATION_SOFT_FAIL, not a silent success deep inside verification.
    logger.warning(f"Unsupported public key type for OCSP verification: {type(public_key)}")
    return False


def _verify_ocsp_signature(
    ocsp_response: ocsp.OCSPResponse,
    issuer_cert: x509.Certificate,
    serial_str: str,
) -> bool:
    """
    Verify the signature on an OCSP response.

    The OCSP response must be signed by either:
    1. The issuer CA directly, or
    2. A delegated OCSP responder certificate issued by the CA

    Args:
        ocsp_response: The parsed OCSP response
        issuer_cert: The issuer (CA) certificate
        serial_str: Certificate serial number (for logging)

    Returns:
        True if signature is valid, False otherwise
    """
    try:
        # Get and validate the responder certificate
        verify_cert = _get_ocsp_responder_cert(ocsp_response, issuer_cert, serial_str)
        if verify_cert is None:
            return False

        # Get the signature components
        public_key = verify_cert.public_key()
        sig_alg_oid = ocsp_response.signature_algorithm_oid
        signature = ocsp_response.signature
        tbs_data = ocsp_response.tbs_response_bytes

        # Determine the hash algorithm from the signature algorithm OID
        hash_algorithm = _get_hash_algorithm_for_sig_oid(sig_alg_oid)
        if hash_algorithm is None:
            # FAIL CLOSED — see _verify_signature_by_key_type.
            logger.warning(
                f"Unsupported OCSP signature algorithm {sig_alg_oid} for serial {serial_str}"
            )
            return False

        # Verify the signature based on key type
        if not _verify_signature_by_key_type(
            public_key, signature, tbs_data, hash_algorithm, serial_str
        ):
            return False

        logger.debug(f"OCSP response signature verified for serial {serial_str}")
        return True

    except InvalidSignature:
        logger.warning(f"OCSP response signature verification failed for serial {serial_str}")
        return False
    except Exception:
        # FAIL CLOSED. This used to return True ("soft-fail"), which told the
        # caller an *unverified* OCSP response was authentic. OCSP is fetched
        # over plain HTTP, so a forged GOOD response — or any malformed one that
        # made this block raise — was accepted as proof of non-revocation and
        # cached for PKI_CRL_CACHE_SECONDS. Because a False (= not revoked) OCSP
        # result returns immediately from _check_revocation, that also skipped
        # the CRL cross-check and defeated PKI_REVOCATION_SOFT_FAIL=false.
        # Returning False makes the OCSP check inconclusive, which is what the
        # CRL fallback and the soft-fail setting exist to handle.
        logger.exception(
            f"Could not verify OCSP response signature for serial {serial_str}; "
            "treating the response as unverified"
        )
        return False


def _get_ocsp_url(cert: x509.Certificate) -> str | None:
    """
    Extract OCSP responder URL from certificate.

    Args:
        cert: The certificate to extract URL from

    Returns:
        OCSP URL string or None if not found
    """
    try:
        aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS)
        ocsp_urls = [
            desc.access_location.value  # type: ignore[attr-defined]
            for desc in aia.value  # type: ignore[attr-defined]
            if desc.access_method == AuthorityInformationAccessOID.OCSP
        ]
        if not ocsp_urls:
            logger.debug("No OCSP responder URL found in certificate")
            return None
        logger.debug(f"OCSP responder URL: {ocsp_urls[0]}")
        return str(ocsp_urls[0])
    except x509.ExtensionNotFound:
        logger.debug("Authority Information Access extension not found")
        return None


def _build_ocsp_request(
    cert: x509.Certificate,
    issuer_cert: x509.Certificate,
) -> bytes | None:
    """
    Build an OCSP request for the given certificate.

    Args:
        cert: The certificate to check
        issuer_cert: The issuer's certificate

    Returns:
        DER-encoded OCSP request bytes or None on failure
    """
    try:
        builder = ocsp.OCSPRequestBuilder()
        builder = builder.add_certificate(cert, issuer_cert, hashes.SHA256())
        ocsp_request = builder.build()
        return ocsp_request.public_bytes(serialization.Encoding.DER)
    except Exception as e:
        logger.error(f"Failed to build OCSP request: {e}")
        return None


def _send_ocsp_request(ocsp_url: str, ocsp_request_data: bytes) -> bytes | None:
    """
    Send an OCSP request and return the response.

    Args:
        ocsp_url: The OCSP responder URL
        ocsp_request_data: DER-encoded OCSP request

    Returns:
        Response content bytes or None on failure
    """
    timeout_seconds = get_process_auth_settings().pki_ocsp_timeout_seconds
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(
                ocsp_url,
                content=ocsp_request_data,
                headers={"Content-Type": "application/ocsp-request"},
            )
            if response.status_code != 200:
                logger.warning(f"OCSP responder returned status {response.status_code}")
                return None
            return response.content  # type: ignore[no-any-return]
    except httpx.TimeoutException:
        logger.warning(f"OCSP request timed out after {timeout_seconds}s")
        return None
    except httpx.RequestError as e:
        logger.warning(f"OCSP request failed: {e}")
        return None


def _cert_id_matches(
    single: ocsp.OCSPSingleResponse,
    cert: x509.Certificate,
    issuer_cert: x509.Certificate,
) -> bool:
    """Whether a SingleResponse's CertID names ``cert`` as issued by ``issuer_cert``."""
    if single.serial_number != cert.serial_number:
        return False
    # An unsupported CertID hash raises; the caller treats that as inconclusive.
    expected = (
        ocsp.OCSPRequestBuilder().add_certificate(cert, issuer_cert, single.hash_algorithm).build()
    )
    return (
        single.issuer_name_hash == expected.issuer_name_hash
        and single.issuer_key_hash == expected.issuer_key_hash
    )


def _good_cache_ttl(single: ocsp.OCSPSingleResponse, now: datetime) -> int | None:
    """Seconds a GOOD answer may be cached, or None when it has expired.

    It expires at ``next_update`` (tolerating ``_OCSP_CLOCK_SKEW``), or
    ``_OCSP_MAX_AGE_WITHOUT_NEXT_UPDATE`` after ``this_update`` when the responder sets no
    ``next_update``. It is never cached past that point.
    """
    next_update = single.next_update_utc
    if next_update is not None:
        if next_update + _OCSP_CLOCK_SKEW <= now:
            return None
        cache_until = next_update
    else:
        cache_until = single.this_update_utc + _OCSP_MAX_AGE_WITHOUT_NEXT_UPDATE
        if cache_until <= now:
            return None
    remaining = int((cache_until - now).total_seconds())
    return max(0, min(remaining, get_process_auth_settings().pki_crl_cache_seconds))


def _parse_ocsp_response(
    ocsp_response_data: bytes,
    issuer_cert: x509.Certificate,
    cert: x509.Certificate,
) -> bool | None:
    """
    Parse OCSP response and determine revocation status.

    The answer counts only when its CertID names ``cert`` (serial, issuer name hash and
    issuer key hash) and it is inside its validity window; anything else is
    inconclusive, which the CRL fallback and ``pki_revocation_soft_fail`` decide.

    Args:
        ocsp_response_data: Raw OCSP response bytes
        issuer_cert: The issuer's certificate for signature verification
        cert: The certificate the request was made for

    Returns:
        True if revoked, False if valid, None if check failed
    """
    serial_str = str(cert.serial_number)
    try:
        ocsp_response = ocsp.load_der_ocsp_response(ocsp_response_data)

        if ocsp_response.response_status != ocsp.OCSPResponseStatus.SUCCESSFUL:
            logger.warning(f"OCSP response status: {ocsp_response.response_status.name}")
            return None

        # Verify OCSP response signature
        if not _verify_ocsp_signature(ocsp_response, issuer_cert, serial_str):
            return None

        single = next(
            (r for r in ocsp_response.responses if _cert_id_matches(r, cert, issuer_cert)),
            None,
        )
        if single is None:
            logger.warning(f"OCSP response does not cover certificate serial {serial_str}")
            return None

        now = datetime.now(UTC)
        if single.this_update_utc > now + _OCSP_CLOCK_SKEW:
            logger.warning(f"OCSP response for serial {serial_str} is not yet valid")
            return None

        cert_status = single.certificate_status

        if cert_status == ocsp.OCSPCertStatus.REVOKED:
            logger.info(
                f"Certificate {serial_str} is REVOKED "
                f"(revocation time: {single.revocation_time_utc})"
            )
            _set_ocsp_cache(serial_str, True, get_process_auth_settings().pki_crl_cache_seconds)
            return True

        if cert_status == ocsp.OCSPCertStatus.GOOD:
            ttl = _good_cache_ttl(single, now)
            if ttl is None:
                logger.warning(f"OCSP GOOD for serial {serial_str} is stale; ignoring it")
                return None
            _set_ocsp_cache(serial_str, False, ttl)
            logger.debug(f"Certificate {serial_str} is valid (OCSP GOOD)")
            return False

        # UNKNOWN status
        logger.warning(f"OCSP response status UNKNOWN for serial {serial_str}")
        return None

    except Exception as e:
        logger.error(f"Failed to parse OCSP response: {e}")
        return None


def _check_ocsp(cert: x509.Certificate, issuer_cert: x509.Certificate) -> bool | None:
    """
    Check certificate revocation status via OCSP.

    Args:
        cert: The certificate to check
        issuer_cert: The issuer's certificate (for building OCSP request)

    Returns:
        True if revoked, False if valid, None if check failed
    """
    serial_str = str(cert.serial_number)

    # Check cache first
    cached_result = _get_ocsp_cache(serial_str)
    if cached_result is not None:
        logger.debug(f"OCSP cache hit for serial {serial_str}: revoked={cached_result}")
        return cached_result

    # Extract OCSP responder URL
    ocsp_url = _get_ocsp_url(cert)
    if ocsp_url is None:
        return None

    # Build OCSP request
    ocsp_request_data = _build_ocsp_request(cert, issuer_cert)
    if ocsp_request_data is None:
        return None

    # Send OCSP request
    ocsp_response_data = _send_ocsp_request(ocsp_url, ocsp_request_data)
    if ocsp_response_data is None:
        return None

    # Parse and return result
    return _parse_ocsp_response(ocsp_response_data, issuer_cert, cert)
