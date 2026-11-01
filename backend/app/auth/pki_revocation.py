"""Certificate revocation decision for PKI login: OCSP first, then CRL, then soft-fail.

``_check_revocation`` loads the configured issuer CA, asks OCSP (``auth/pki_ocsp.py``),
falls back to the CA-signature-verified CRL from the certificate's distribution points
(cached per URL), and when both are inconclusive lets ``pki_revocation_soft_fail``
decide. Called from ``auth/pki_auth.py``'s ``_handle_revocation_check``.
"""

import logging
import time
from collections import OrderedDict

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import ExtensionOID

from app.auth.pki_ocsp import _cache_lock
from app.auth.pki_ocsp import _check_ocsp
from app.core.config import settings
from app.core.process_auth_settings import get_process_auth_settings

logger = logging.getLogger(__name__)

_crl_cache: OrderedDict[str, tuple[x509.CertificateRevocationList, float]] = (
    OrderedDict()
)  # url -> (crl, expiry_time)


def _evict_lru_crl_cache() -> None:
    """Evict least recently used entries from CRL cache if over max size.

    Must be called with _cache_lock held.
    """
    max_size = settings.PKI_CRL_CACHE_MAX_SIZE
    while len(_crl_cache) > max_size:
        # Pop the first (oldest) item
        evicted_key, _ = _crl_cache.popitem(last=False)
        logger.debug(f"CRL cache LRU eviction: removed entry for {evicted_key}")


def _get_crl_cache(url: str) -> x509.CertificateRevocationList | None:
    """
    Get cached CRL for a distribution point URL.

    Uses LRU eviction - accessed entries are moved to end of cache.

    Args:
        url: CRL distribution point URL

    Returns:
        Cached CRL or None if not cached or expired
    """
    with _cache_lock:
        if url in _crl_cache:
            crl, expiry_time = _crl_cache[url]
            if time.time() < expiry_time:
                # Move to end (most recently used) for LRU behavior
                _crl_cache.move_to_end(url)
                return crl
            # Expired, remove from cache
            del _crl_cache[url]
    return None


def _set_crl_cache(url: str, crl: x509.CertificateRevocationList) -> None:
    """
    Cache CRL for a distribution point URL.

    Uses LRU eviction when cache exceeds PKI_CRL_CACHE_MAX_SIZE.

    Args:
        url: CRL distribution point URL
        crl: The CRL to cache
    """
    ttl = get_process_auth_settings().pki_crl_cache_seconds
    with _cache_lock:
        # If key already exists, move to end
        if url in _crl_cache:
            _crl_cache.move_to_end(url)
        _crl_cache[url] = (crl, time.time() + ttl)
        # Evict oldest entries if over max size
        _evict_lru_crl_cache()


def _load_issuer_certificate() -> x509.Certificate | None:
    """
    Load the issuer (CA) certificate from the configured path.

    Returns:
        Issuer certificate or None if not configured or failed to load
    """
    ca_cert_path = get_process_auth_settings().pki_ca_cert_path
    if not ca_cert_path:
        logger.debug("pki_ca_cert_path not configured")
        return None

    try:
        with open(ca_cert_path, "rb") as f:
            ca_pem = f.read()
        return x509.load_pem_x509_certificate(ca_pem, default_backend())
    except FileNotFoundError:
        logger.error(f"CA certificate file not found: {ca_cert_path}")
        return None
    except Exception as e:
        logger.error(f"Failed to load CA certificate: {e}")
        return None


def _verify_crl_signature(
    crl: x509.CertificateRevocationList,
    issuer_cert: x509.Certificate,
) -> bool:
    """
    Verify the signature on a CRL using the issuer certificate.

    Args:
        crl: The CRL to verify
        issuer_cert: The issuer (CA) certificate

    Returns:
        True if signature is valid, False otherwise
    """
    try:
        # The cryptography library provides is_signature_valid method for CRL
        # This verifies the CRL was signed by the issuer certificate's public key
        if crl.is_signature_valid(issuer_cert.public_key()):  # type: ignore[arg-type]
            logger.debug("CRL signature verified successfully")
            return True
        else:
            logger.warning("CRL signature is invalid")
            return False
    except InvalidSignature:
        logger.warning("CRL signature verification failed: invalid signature")
        return False
    except Exception as e:
        logger.warning(f"CRL signature verification error: {e}")
        return False


def _get_crl_urls(cert: x509.Certificate) -> list[str] | None:
    """
    Extract CRL distribution point URLs from certificate.

    Args:
        cert: The certificate to extract URLs from

    Returns:
        List of CRL URLs or None if not found
    """
    try:
        crl_dp = cert.extensions.get_extension_for_oid(ExtensionOID.CRL_DISTRIBUTION_POINTS)
        crl_urls = []
        for dp in crl_dp.value:  # type: ignore[attr-defined]
            if dp.full_name:
                for name in dp.full_name:
                    if isinstance(name, x509.UniformResourceIdentifier):
                        crl_urls.append(str(name.value))
        if not crl_urls:
            logger.debug("No CRL distribution point URLs found in certificate")
            return None
        logger.debug(f"CRL distribution points: {crl_urls}")
        return crl_urls
    except x509.ExtensionNotFound:
        logger.debug("CRL Distribution Points extension not found")
        return None


def _download_crl(crl_url: str) -> bytes | None:
    """
    Download CRL data from a distribution point.

    Args:
        crl_url: The CRL distribution point URL

    Returns:
        Raw CRL data bytes or None on failure
    """
    try:
        # Use a longer timeout for CRL downloads as they can be large
        with httpx.Client(
            timeout=get_process_auth_settings().pki_ocsp_timeout_seconds * 2
        ) as client:
            response = client.get(crl_url)
            if response.status_code != 200:
                logger.warning(f"CRL download failed for {crl_url}: status {response.status_code}")
                return None
            return response.content  # type: ignore[no-any-return]
    except httpx.TimeoutException:
        logger.warning(f"CRL download timed out for {crl_url}")
        return None
    except httpx.RequestError as e:
        logger.warning(f"CRL download failed for {crl_url}: {e}")
        return None


def _parse_and_verify_crl(
    crl_data: bytes,
    crl_url: str,
    issuer_cert: x509.Certificate | None,
) -> x509.CertificateRevocationList | None:
    """
    Parse CRL data and verify its signature.

    Args:
        crl_data: Raw CRL data bytes
        crl_url: The CRL URL (for logging)
        issuer_cert: Optional issuer certificate for signature verification

    Returns:
        Parsed CRL or None on failure
    """
    try:
        # Try DER format first (most common for CRL distribution points)
        try:
            crl = x509.load_der_x509_crl(crl_data, default_backend())
        except Exception:
            # Fall back to PEM format
            crl = x509.load_pem_x509_crl(crl_data, default_backend())

        # Verify CRL signature before trusting its contents. FAIL CLOSED when we
        # cannot: a CRL is downloaded over the network and drives an
        # authentication decision, so an unverifiable one must be treated as no
        # CRL at all (-> inconclusive -> PKI_REVOCATION_SOFT_FAIL decides).
        # `validate_pki_settings` already requires PKI_CA_CERT_PATH whenever
        # PKI_VERIFY_REVOCATION is on, so a missing issuer here means the CA file
        # is unreadable — an operator problem, not a reason to trust the CRL.
        if issuer_cert is None:
            logger.error(
                f"No issuer certificate available to verify the CRL from {crl_url}; "
                "refusing to trust it"
            )
            return None
        if not _verify_crl_signature(crl, issuer_cert):
            logger.warning(f"CRL signature verification failed for {crl_url}")
            return None

        return crl
    except Exception as e:
        logger.warning(f"Failed to parse CRL from {crl_url}: {e}")
        return None


def _get_or_download_crl(
    crl_url: str,
    issuer_cert: x509.Certificate | None,
) -> x509.CertificateRevocationList | None:
    """
    Get CRL from cache or download and cache it.

    Args:
        crl_url: The CRL distribution point URL
        issuer_cert: Optional issuer certificate for signature verification

    Returns:
        CRL or None on failure
    """
    # Check cache first
    cached_crl = _get_crl_cache(crl_url)
    if cached_crl:
        logger.debug(f"CRL cache hit for {crl_url}")
        return cached_crl

    # Download CRL
    crl_data = _download_crl(crl_url)
    if crl_data is None:
        return None

    # Parse and verify CRL
    crl = _parse_and_verify_crl(crl_data, crl_url, issuer_cert)
    if crl is None:
        return None

    # Cache the CRL
    _set_crl_cache(crl_url, crl)
    logger.debug(f"CRL cached from {crl_url}")
    return crl


def _check_crl(
    cert: x509.Certificate,
    issuer_cert: x509.Certificate | None = None,
) -> bool | None:
    """
    Check certificate revocation status via CRL.

    Args:
        cert: The certificate to check
        issuer_cert: Optional issuer certificate for CRL signature verification

    Returns:
        True if revoked, False if valid, None if check failed
    """
    # Extract CRL Distribution Points
    crl_urls = _get_crl_urls(cert)
    if crl_urls is None:
        return None

    serial_number = cert.serial_number

    # Try each CRL URL until one works
    for crl_url in crl_urls:
        crl = _get_or_download_crl(crl_url, issuer_cert)
        if crl is None:
            continue

        # Check if certificate serial number is in the CRL
        revoked_cert = crl.get_revoked_certificate_by_serial_number(serial_number)
        if revoked_cert:
            logger.info(
                f"Certificate {serial_number} found in CRL "
                f"(revocation date: {revoked_cert.revocation_date})"
            )
            return True

        # Certificate not in CRL - it's valid
        logger.debug(f"Certificate {serial_number} not in CRL - valid")
        return False

    # All CRL URLs failed
    logger.warning("All CRL distribution point downloads failed")
    return None


def _check_revocation(cert: x509.Certificate) -> tuple[bool, str]:
    """
    Check certificate revocation status using OCSP (preferred) then CRL fallback.

    Args:
        cert: The certificate to check

    Returns:
        Tuple of (is_revoked, reason)
        - is_revoked: True if certificate is revoked, False if valid or soft-fail
        - reason: Human-readable reason string
    """
    serial_str = str(cert.serial_number)
    logger.debug(f"Checking revocation for certificate serial {serial_str}")

    # Load issuer certificate for OCSP
    issuer_cert = _load_issuer_certificate()

    # Try OCSP first (real-time, preferred)
    if issuer_cert:
        ocsp_result = _check_ocsp(cert, issuer_cert)
        if ocsp_result is True:
            return (True, "Certificate revoked (OCSP)")
        elif ocsp_result is False:
            return (False, "Certificate valid (OCSP)")
        # OCSP check failed, try CRL
        logger.debug("OCSP check inconclusive, falling back to CRL")
    else:
        logger.debug("Issuer certificate not available, skipping OCSP, trying CRL")

    # Fall back to CRL (pass issuer cert for signature verification)
    crl_result = _check_crl(cert, issuer_cert)
    if crl_result is True:
        return (True, "Certificate revoked (CRL)")
    elif crl_result is False:
        return (False, "Certificate valid (CRL)")

    # Both checks failed
    if get_process_auth_settings().pki_revocation_soft_fail:
        logger.warning(
            f"Revocation check failed for serial {serial_str}, "
            "allowing authentication (soft-fail enabled)"
        )
        return (False, "Revocation check failed (soft-fail)")
    else:
        logger.error(
            f"Revocation check failed for serial {serial_str}, "
            "denying authentication (soft-fail disabled)"
        )
        return (True, "Revocation check failed (hard-fail)")
