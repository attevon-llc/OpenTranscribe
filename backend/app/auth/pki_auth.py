"""
PKI/X.509 certificate authentication module.

Handles authentication via client certificates (mutual TLS).
The reverse proxy (Nginx) handles TLS termination and passes
certificate information via headers.

Supports certificate revocation checking via:
- OCSP (Online Certificate Status Protocol) - preferred, real-time
- CRL (Certificate Revocation List) - fallback when OCSP unavailable
"""

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC
from typing import TypedDict
from urllib.parse import unquote

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.x509 import ocsp
from cryptography.x509.oid import AuthorityInformationAccessOID
from cryptography.x509.oid import ExtensionOID

from app.auth.constants import AUTH_TYPE_PKI
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.core.config import settings

logger = logging.getLogger(__name__)

# ===== Trusted Proxy Validation =====


def _parse_pki_trusted_proxies(trusted_proxies_str: str) -> list:
    """
    Parse trusted proxies configuration string into a list of IP networks.

    Args:
        trusted_proxies_str: Comma-separated list of IPs or CIDR ranges

    Returns:
        List of parsed IP networks
    """
    import ipaddress

    if not trusted_proxies_str:
        return []

    networks = []
    for proxy in trusted_proxies_str.split(","):
        proxy = proxy.strip()
        if not proxy:
            continue
        try:
            # Try parsing as a network (CIDR notation)
            if "/" in proxy:
                networks.append(ipaddress.ip_network(proxy, strict=False))
            else:
                # Single IP - convert to /32 or /128 network
                ip = ipaddress.ip_address(proxy)
                if isinstance(ip, ipaddress.IPv4Address):
                    networks.append(ipaddress.ip_network(f"{proxy}/32"))
                else:
                    networks.append(ipaddress.ip_network(f"{proxy}/128"))
        except ValueError as e:
            logger.warning(f"Invalid PKI trusted proxy address '{proxy}': {e}")
    return networks


def _is_pki_trusted_proxy(client_ip: str, trusted_networks: list) -> bool:
    """
    Check if an IP address is in the list of trusted proxy networks.

    Args:
        client_ip: IP address to check
        trusted_networks: List of trusted IP networks

    Returns:
        True if the IP is trusted, False otherwise
    """
    import ipaddress

    if not trusted_networks:
        return False

    try:
        ip = ipaddress.ip_address(client_ip)
        for network in trusted_networks:
            if ip in network:
                return True
    except ValueError:
        logger.warning(f"Invalid IP address format: {client_ip}")
    return False


def _get_pki_client_ip(request) -> str:
    """
    Get the client IP from a request.

    Args:
        request: FastAPI Request object

    Returns:
        Client IP address or "unknown"
    """
    if hasattr(request, "client") and request.client:
        return request.client.host or "unknown"
    return "unknown"


# Parse trusted proxies at module load time for efficiency
_pki_trusted_proxy_networks = _parse_pki_trusted_proxies(settings.PKI_TRUSTED_PROXIES)


# ===== Revocation Checking Cache =====
# Thread-safe LRU cache for OCSP responses and CRLs with configurable max size

_cache_lock = threading.Lock()
# Using OrderedDict for LRU behavior - most recently used items are moved to end
_ocsp_cache: OrderedDict[str, tuple[bool, float]] = (
    OrderedDict()
)  # serial_number -> (is_revoked, expiry_time)
_crl_cache: OrderedDict[str, tuple[x509.CertificateRevocationList, float]] = (
    OrderedDict()
)  # url -> (crl, expiry_time)


def _evict_lru_ocsp_cache() -> None:
    """Evict least recently used entries from OCSP cache if over max size.

    Must be called with _cache_lock held.
    """
    max_size = settings.PKI_OCSP_CACHE_MAX_SIZE
    while len(_ocsp_cache) > max_size:
        # Pop the first (oldest) item
        evicted_key, _ = _ocsp_cache.popitem(last=False)
        logger.debug(f"OCSP cache LRU eviction: removed entry for serial {evicted_key}")


def _evict_lru_crl_cache() -> None:
    """Evict least recently used entries from CRL cache if over max size.

    Must be called with _cache_lock held.
    """
    max_size = settings.PKI_CRL_CACHE_MAX_SIZE
    while len(_crl_cache) > max_size:
        # Pop the first (oldest) item
        evicted_key, _ = _crl_cache.popitem(last=False)
        logger.debug(f"CRL cache LRU eviction: removed entry for {evicted_key}")


class PKIUserData(TypedDict):
    """User data extracted from X.509 certificate."""

    subject_dn: str  # Full Distinguished Name
    common_name: str  # CN from certificate
    email: str  # Email from certificate (if present)
    is_admin: bool
    # Certificate metadata (optional - populated when full cert is available)
    serial_number: str | None  # Hex format
    issuer_dn: str | None
    organization: str | None
    organizational_unit: str | None
    not_before: str | None  # ISO format
    not_after: str | None  # ISO format
    fingerprint: str | None  # SHA-256, colon-separated hex


@dataclass
class CertificateInfo:
    """Parsed certificate information."""

    subject_dn: str
    common_name: str
    email: str | None
    organization: str | None
    organizational_unit: str | None
    serial_number: str  # Hex format
    issuer_dn: str
    not_before: str  # ISO format
    not_after: str  # ISO format
    fingerprint: str  # SHA-256, colon-separated hex


def _extract_certificate_metadata(cert: x509.Certificate) -> dict:
    """Extract comprehensive metadata from X.509 certificate.

    Args:
        cert: The parsed X.509 certificate

    Returns:
        Dictionary with certificate metadata including serial number (hex),
        issuer DN, organization, organizational unit, validity dates (ISO format),
        and SHA-256 fingerprint (colon-separated hex)
    """
    # Serial number in hex format
    serial_hex = format(cert.serial_number, "X")

    # Issuer DN
    issuer_dn = cert.issuer.rfc4514_string()

    # Organization and OU from subject
    org = None
    ou = None
    for attr in cert.subject:
        if attr.oid == x509.oid.NameOID.ORGANIZATION_NAME:
            org = str(attr.value)
        elif attr.oid == x509.oid.NameOID.ORGANIZATIONAL_UNIT_NAME:
            ou = str(attr.value)

    # Fingerprint (SHA-256) - colon-separated hex pairs
    fingerprint = cert.fingerprint(hashes.SHA256()).hex().upper()
    fingerprint_formatted = ":".join(fingerprint[i : i + 2] for i in range(0, len(fingerprint), 2))

    return {
        "serial_number": serial_hex,
        "issuer_dn": issuer_dn,
        "organization": org,
        "organizational_unit": ou,
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "fingerprint": fingerprint_formatted,
    }


def extract_display_name_from_gov_dn(subject_dn: str) -> str:
    """
    Extract a user-friendly display name from government certificate DNs.

    Supports common government certificate formats:
    - DoD CAC: CN=LAST.FIRST.MIDDLE.EDIPI (e.g., CN=SMITH.JOHN.WILLIAM.1234567890)
    - PIV: CN=LAST.FIRST.M or CN=LAST.FIRST (e.g., CN=SMITH.JOHN.W)

    Args:
        subject_dn: Certificate subject Distinguished Name string

    Returns:
        User-friendly display name (e.g., "John William Smith" or "John W. Smith"),
        or the original CN if pattern doesn't match
    """
    import re

    # Extract CN from DN
    cn_match = re.search(r"CN=([^,]+)", subject_dn, re.IGNORECASE)
    if not cn_match:
        return subject_dn

    cn = cn_match.group(1)

    # DoD CAC format: LAST.FIRST.MIDDLE.EDIPI (10-digit EDIPI)
    dod_match = re.match(r"^([A-Z]+)\.([A-Z]+)\.([A-Z]*)\.(\d{10})$", cn, re.IGNORECASE)
    if dod_match:
        last, first, middle, _edipi = dod_match.groups()
        if middle:
            return f"{first.title()} {middle.title()}. {last.title()}"
        return f"{first.title()} {last.title()}"

    # PIV format: LAST.FIRST.M or LAST.FIRST
    piv_match = re.match(r"^([A-Z]+)\.([A-Z]+)\.?([A-Z]?)$", cn, re.IGNORECASE)
    if piv_match:
        last, first, middle = piv_match.groups()
        if middle:
            return f"{first.title()} {middle.upper()}. {last.title()}"
        return f"{first.title()} {last.title()}"

    # Keycloak X.509 / government space-separated format:
    # "LastName FirstName emailusername" (three space-separated tokens)
    # The third token is the email username (no @domain) used for account lookup.
    parts = cn.split()
    if len(parts) == 3:
        last, first, _email_user = parts
        return f"{first.title()} {last.title()}"

    # Default: return CN as-is
    return cn


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
    ttl = settings.PKI_CRL_CACHE_SECONDS
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
    if not settings.PKI_CA_CERT_PATH:
        logger.debug("PKI_CA_CERT_PATH not configured")
        return None

    try:
        with open(settings.PKI_CA_CERT_PATH, "rb") as f:
            ca_pem = f.read()
        return x509.load_pem_x509_certificate(ca_pem, default_backend())
    except FileNotFoundError:
        logger.error(f"CA certificate file not found: {settings.PKI_CA_CERT_PATH}")
        return None
    except Exception as e:
        logger.error(f"Failed to load CA certificate: {e}")
        return None


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

    logger.warning(f"Unsupported public key type for OCSP verification: {type(public_key)}")
    # Allow verification to proceed as soft-fail for unknown key types
    return True


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
            logger.warning(
                f"Unsupported OCSP signature algorithm {sig_alg_oid} for serial {serial_str}"
            )
            # Allow verification to proceed as soft-fail for unknown algorithms
            return True

        # Verify the signature based on key type
        _verify_signature_by_key_type(public_key, signature, tbs_data, hash_algorithm, serial_str)

        logger.debug(f"OCSP response signature verified for serial {serial_str}")
        return True

    except InvalidSignature:
        logger.warning(f"OCSP response signature verification failed for serial {serial_str}")
        return False
    except Exception as e:
        logger.warning(f"Could not verify OCSP response signature for serial {serial_str}: {e}")
        # Soft-fail: allow verification to proceed if we encounter unexpected errors
        # This handles edge cases like unusual signature algorithms
        return True


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
    try:
        with httpx.Client(timeout=settings.PKI_OCSP_TIMEOUT_SECONDS) as client:
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
        logger.warning(f"OCSP request timed out after {settings.PKI_OCSP_TIMEOUT_SECONDS}s")
        return None
    except httpx.RequestError as e:
        logger.warning(f"OCSP request failed: {e}")
        return None


def _parse_ocsp_response(
    ocsp_response_data: bytes,
    issuer_cert: x509.Certificate,
    serial_str: str,
) -> bool | None:
    """
    Parse OCSP response and determine revocation status.

    Args:
        ocsp_response_data: Raw OCSP response bytes
        issuer_cert: The issuer's certificate for signature verification
        serial_str: Certificate serial number for logging and caching

    Returns:
        True if revoked, False if valid, None if check failed
    """
    try:
        ocsp_response = ocsp.load_der_ocsp_response(ocsp_response_data)

        if ocsp_response.response_status != ocsp.OCSPResponseStatus.SUCCESSFUL:
            logger.warning(f"OCSP response status: {ocsp_response.response_status.name}")
            return None

        # Verify OCSP response signature
        if not _verify_ocsp_signature(ocsp_response, issuer_cert, serial_str):
            return None

        cert_status = ocsp_response.certificate_status

        if cert_status == ocsp.OCSPCertStatus.REVOKED:
            logger.info(
                f"Certificate {serial_str} is REVOKED "
                f"(revocation time: {ocsp_response.revocation_time})"
            )
            _set_ocsp_cache(serial_str, True, settings.PKI_CRL_CACHE_SECONDS)
            return True

        if cert_status == ocsp.OCSPCertStatus.GOOD:
            # Calculate TTL from response or use default
            ttl = 300  # 5 minutes default
            if ocsp_response.next_update:
                ttl_from_response = (
                    ocsp_response.next_update - ocsp_response.this_update
                ).total_seconds()
                ttl = min(int(ttl_from_response), settings.PKI_CRL_CACHE_SECONDS)
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
    return _parse_ocsp_response(ocsp_response_data, issuer_cert, serial_str)


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
        with httpx.Client(timeout=settings.PKI_OCSP_TIMEOUT_SECONDS * 2) as client:
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

        # Verify CRL signature before trusting its contents
        if issuer_cert:
            if not _verify_crl_signature(crl, issuer_cert):
                logger.warning(f"CRL signature verification failed for {crl_url}")
                return None
        else:
            logger.warning(
                f"No issuer certificate available for CRL signature verification "
                f"({crl_url}) - proceeding without verification"
            )

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
    if settings.PKI_REVOCATION_SOFT_FAIL:
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


def extract_certificate_from_headers(request) -> str | None:
    """
    Extract client certificate from request headers.

    The reverse proxy (Nginx) must be configured to pass the
    client certificate via headers.

    Args:
        request: FastAPI Request object

    Returns:
        PEM-encoded certificate string or None
    """
    cert_header = request.headers.get(settings.PKI_CERT_HEADER)
    if not cert_header:
        return None

    # Nginx URL-encodes the certificate
    return unquote(cert_header)


def extract_dn_from_headers(request) -> str | None:
    """
    Extract Distinguished Name from headers (if Nginx provides it).

    Args:
        request: FastAPI Request object

    Returns:
        DN string or None
    """
    result = request.headers.get(settings.PKI_CERT_DN_HEADER)
    return str(result) if result is not None else None


def parse_certificate(cert_pem: str) -> CertificateInfo | None:
    """
    Parse X.509 certificate and extract comprehensive information.

    Extracts all certificate metadata including subject DN, issuer DN,
    organization, organizational unit, validity dates, serial number
    (in hex format), and SHA-256 fingerprint.

    Args:
        cert_pem: PEM-encoded certificate string

    Returns:
        CertificateInfo with all metadata or None if parsing fails
    """
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())

        subject = cert.subject

        # Extract common name
        cn_attrs = subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        common_name = str(cn_attrs[0].value) if cn_attrs else ""

        # Extract email (may be in subject or SAN)
        email: str | None = None
        email_attrs = subject.get_attributes_for_oid(x509.oid.NameOID.EMAIL_ADDRESS)
        if email_attrs:
            email = str(email_attrs[0].value)
        else:
            # Try Subject Alternative Name
            try:
                san = cert.extensions.get_extension_for_oid(
                    x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME
                )
                for name in san.value:  # type: ignore[attr-defined]
                    if isinstance(name, x509.RFC822Name):
                        email = str(name.value)
                        break
            except x509.ExtensionNotFound:
                pass

        # Extract comprehensive metadata using helper function
        metadata = _extract_certificate_metadata(cert)

        return CertificateInfo(
            subject_dn=subject.rfc4514_string(),
            common_name=common_name,
            email=email,
            organization=metadata["organization"],
            organizational_unit=metadata["organizational_unit"],
            serial_number=metadata["serial_number"],
            issuer_dn=metadata["issuer_dn"],
            not_before=metadata["not_before"],
            not_after=metadata["not_after"],
            fingerprint=metadata["fingerprint"],
        )
    except Exception as e:
        logger.error(f"Failed to parse certificate: {e}")
        return None


def _parse_dn_components(dn: str) -> tuple[str, str]:
    """
    Parse DN string to extract CN and email.

    Args:
        dn: Distinguished Name string (e.g., "CN=John Doe,O=Org,C=US")

    Returns:
        Tuple of (common_name, email)
    """
    common_name = ""
    email = ""

    # DN format can vary: CN=John Doe,O=Org,C=US or CN=John Doe, O=Org, C=US
    for part in dn.split(","):
        part = part.strip()
        if part.upper().startswith("CN="):
            common_name = part[3:].strip()
        elif part.upper().startswith("EMAILADDRESS="):
            email = part[13:].strip()
        elif part.upper().startswith("E="):
            email = part[2:].strip()

    return common_name, email


def _normalize_dn(dn: str | None) -> str:
    """
    Normalize a Distinguished Name for case-insensitive comparison.

    DNs are normalized by:
    1. Converting to lowercase
    2. Stripping whitespace from the full DN
    3. Normalizing whitespace around RDN separators (commas)
    4. Normalizing whitespace around attribute separators (=)

    Example:
        "CN=John Doe, O=My Org, C=US" -> "cn=john doe,o=my org,c=us"

    Args:
        dn: Distinguished Name string or None

    Returns:
        Normalized DN string for comparison (empty string if dn is None or empty)
    """
    if not dn:
        return ""

    # Split by comma, normalize each RDN, then rejoin
    rdns = []
    for rdn in dn.split(","):
        rdn = rdn.strip()
        if "=" in rdn:
            # Split on first = only to handle values containing =
            attr, value = rdn.split("=", 1)
            # Normalize: lowercase attribute type, preserve case for values but lowercase for comparison
            rdns.append(f"{attr.strip().lower()}={value.strip().lower()}")
        elif rdn:
            rdns.append(rdn.lower())

    return ",".join(rdns)


def _is_pki_admin(subject_dn: str, admin_dns_config: str | None = None) -> bool:
    """
    Check if certificate DN is in the admin DNs list.

    Performs case-insensitive DN comparison with proper normalization.

    Args:
        subject_dn: Certificate subject DN
        admin_dns_config: Admin DN list (DB-backed config takes precedence
            over .env — callers with a session pass auth_settings.pki_admin_dns).
            Falls back to the PKI_ADMIN_DNS environment setting.

    Returns:
        True if user is an admin
    """
    effective_dns = admin_dns_config if admin_dns_config else settings.PKI_ADMIN_DNS
    if not effective_dns:
        return False

    normalized_subject = _normalize_dn(subject_dn)
    # Admin DNs use semicolons to separate entries because full DNs contain commas.
    # e.g. PKI_ADMIN_DNS=CN=Doe John jdoe,O=U.S. Government,C=US;CN=Smith Jane jsmith,...
    from app.auth.ldap_auth import _parse_group_list

    admin_dns = [_normalize_dn(dn) for dn in _parse_group_list(effective_dns)]

    return normalized_subject in admin_dns


def _validate_pki_headers_source(request) -> bool | None:
    """
    Validate that PKI headers come from a trusted proxy.

    Args:
        request: FastAPI Request object

    Returns:
        True if valid/allowed, False if headers from untrusted source, None to continue
    """
    client_ip = _get_pki_client_ip(request)
    has_pki_headers = request.headers.get(settings.PKI_CERT_HEADER) or request.headers.get(
        settings.PKI_CERT_DN_HEADER
    )

    if _pki_trusted_proxy_networks:
        if not _is_pki_trusted_proxy(client_ip, _pki_trusted_proxy_networks):
            if has_pki_headers:
                logger.warning(
                    f"PKI certificate headers received from untrusted IP {client_ip}. "
                    "This may indicate header injection attempt. "
                    f"Configure PKI_TRUSTED_PROXIES to include legitimate proxy IPs."
                )
                return False
            # No PKI headers present, allow for fallback to other auth methods
            logger.debug(f"No PKI headers from IP {client_ip}, no trusted proxy validation needed")
        return True

    # No trusted proxies configured - log warning about potential risk
    if has_pki_headers:
        logger.warning(
            "SECURITY: PKI_TRUSTED_PROXIES not configured. PKI headers accepted from any source. "
            "This allows potential header injection attacks. "
            "Configure PKI_TRUSTED_PROXIES with your reverse proxy IP addresses."
        )
    return True


def _handle_revocation_check(cert: x509.Certificate | None) -> bool | None:
    """
    Handle certificate revocation checking if enabled.

    Args:
        cert: The parsed certificate (may be None)

    Returns:
        True if authentication should be denied, False if OK to proceed, None if revocation
        checking is disabled
    """
    if not settings.PKI_VERIFY_REVOCATION:
        return False  # Revocation checking disabled, proceed

    if not cert:
        logger.warning(
            "Revocation checking enabled but no certificate provided. "
            "Configure Nginx to pass full certificate via PKI_CERT_HEADER."
        )
        if not settings.PKI_REVOCATION_SOFT_FAIL:
            logger.error("Denying authentication: revocation check required but no certificate")
            return True  # Deny
        logger.warning("Allowing authentication without revocation check (soft-fail)")
        return False  # Allow with soft-fail

    is_revoked, reason = _check_revocation(cert)
    if is_revoked:
        logger.warning(f"Certificate authentication denied: {reason}")
        return True  # Deny
    logger.debug(f"Revocation check passed: {reason}")
    return False  # Allow


def _extract_user_info_from_request(
    request,
    cert: x509.Certificate | None,
    cert_pem: str | None,
) -> tuple[str, str, str] | None:
    """
    Extract user info (subject_dn, common_name, email) from request.

    Args:
        request: FastAPI Request object
        cert: Parsed certificate (may be None)
        cert_pem: PEM-encoded certificate string (may be None)

    Returns:
        Tuple of (subject_dn, common_name, email) or None if extraction fails
    """
    # Try to get DN directly from header (more efficient)
    subject_dn = extract_dn_from_headers(request)

    if subject_dn:
        common_name, email = _parse_dn_components(subject_dn)
        return (subject_dn, common_name, email)

    if cert and cert_pem:
        cert_info = parse_certificate(cert_pem)
        if not cert_info:
            return None
        return (cert_info.subject_dn, cert_info.common_name, cert_info.email or "")

    # No DN header and no certificate
    logger.warning("No client certificate or DN header provided")
    return None


def pki_authenticate(request, admin_dns_config: str | None = None) -> PKIUserData | None:
    """
    Authenticate user via X.509 client certificate.

    Performs certificate validation including optional revocation checking
    via OCSP (preferred) and CRL (fallback) when PKI_VERIFY_REVOCATION is enabled.

    ``admin_dns_config`` carries the DB-backed admin DN list (admin UI takes
    precedence over .env); without it the PKI_ADMIN_DNS env setting is used.

    Args:
        request: FastAPI Request object

    Returns:
        PKIUserData with certificate metadata or None if authentication fails
    """
    # Note: PKI_ENABLED check is done by the caller (pki_login endpoint)
    # which checks the database setting first, then falls back to env.
    # We skip the env-only check here to support database-driven config.

    # Validate that PKI headers come from trusted proxies
    if _validate_pki_headers_source(request) is False:
        return None

    # Extract certificate from headers - needed for revocation checking
    cert_pem = extract_certificate_from_headers(request)
    cert = None
    cert_metadata: dict | None = None

    if cert_pem:
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())
            # Extract comprehensive certificate metadata
            cert_metadata = _extract_certificate_metadata(cert)

            # Validate certificate validity period
            from datetime import datetime

            now = datetime.now(UTC)

            # Check not_before (certificate not yet valid)
            if cert_metadata.get("not_before"):
                not_before = datetime.fromisoformat(
                    cert_metadata["not_before"].replace("Z", "+00:00")
                )
                if now < not_before:
                    logger.warning(
                        f"Certificate not yet valid: valid from {cert_metadata['not_before']}"
                    )
                    return None

            # Check not_after (certificate expired)
            if cert_metadata.get("not_after"):
                not_after = datetime.fromisoformat(
                    cert_metadata["not_after"].replace("Z", "+00:00")
                )
                if now > not_after:
                    logger.warning(f"Certificate expired: expired at {cert_metadata['not_after']}")
                    return None

        except Exception as e:
            logger.error(f"Failed to parse certificate: {e}")
            return None

    # Check revocation status if enabled
    if _handle_revocation_check(cert):
        return None  # Revocation check failed or certificate revoked

    # Extract user information from request
    user_info = _extract_user_info_from_request(request, cert, cert_pem)
    if user_info is None:
        return None

    subject_dn, common_name, email = user_info

    # Check if admin DN (DB-backed config wins over .env when provided)
    is_admin = _is_pki_admin(subject_dn, admin_dns_config)

    logger.info(f"PKI authentication successful for: {subject_dn}")

    return PKIUserData(
        subject_dn=subject_dn,
        common_name=common_name,
        email=email,
        is_admin=is_admin,
        # Include certificate metadata if available
        serial_number=cert_metadata["serial_number"] if cert_metadata else None,
        issuer_dn=cert_metadata["issuer_dn"] if cert_metadata else None,
        organization=cert_metadata["organization"] if cert_metadata else None,
        organizational_unit=cert_metadata["organizational_unit"] if cert_metadata else None,
        not_before=cert_metadata["not_before"] if cert_metadata else None,
        not_after=cert_metadata["not_after"] if cert_metadata else None,
        fingerprint=cert_metadata["fingerprint"] if cert_metadata else None,
    )


def _create_pki_user(db, pki_data: PKIUserData):
    """
    Create a new user from PKI certificate data.

    Args:
        db: Database session
        pki_data: PKI user data

    Returns:
        Created User object

    Raises:
        ValueError: If user cannot be created or found after race condition
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.user import User

    subject_dn = pki_data["subject_dn"]
    email = pki_data["email"]

    # If no email in cert, use CN@pki.local as placeholder
    if not email:
        # Replace spaces with dots for email format
        cn_email_safe = pki_data["common_name"].replace(" ", ".").lower()
        email = f"{cn_email_safe}@pki.local"

    logger.info(f"Creating new user from PKI: {subject_dn}")

    # Parse validity dates from ISO strings if available
    pki_not_before = None
    pki_not_after = None
    not_before_str = pki_data.get("not_before")
    if not_before_str:
        from datetime import datetime

        pki_not_before = datetime.fromisoformat(not_before_str)
    not_after_str = pki_data.get("not_after")
    if not_after_str:
        from datetime import datetime

        pki_not_after = datetime.fromisoformat(not_after_str)

    fingerprint = pki_data.get("fingerprint")
    user = User(
        email=email,
        full_name=pki_data["common_name"] or email.split("@")[0],
        hashed_password=EXTERNAL_AUTH_NO_PASSWORD,
        auth_type=AUTH_TYPE_PKI,
        pki_subject_dn=subject_dn,
        # Certificate metadata fields
        pki_serial_number=pki_data.get("serial_number"),
        pki_issuer_dn=pki_data.get("issuer_dn"),
        pki_organization=pki_data.get("organization"),
        pki_organizational_unit=pki_data.get("organizational_unit"),
        pki_common_name=pki_data["common_name"],
        pki_not_before=pki_not_before,
        pki_not_after=pki_not_after,
        pki_fingerprint_sha256=fingerprint.replace(":", "") if fingerprint else None,
        role="admin" if pki_data["is_admin"] else "user",
        is_active=True,
        # External IdPs grant at most 'admin'; is_superuser mirrors super_admin.
        is_superuser=False,
    )
    db.add(user)

    try:
        db.commit()
        return user
    except IntegrityError:
        # Race condition: user was created by concurrent request
        db.rollback()
        logger.info(
            f"User with DN {subject_dn} was created by concurrent request, fetching existing user"
        )
        user = db.query(User).filter(User.pki_subject_dn == subject_dn).first()
        if not user:
            user = db.query(User).filter(User.email == email).first()
        if not user:
            raise ValueError(f"Failed to create or find PKI user: {subject_dn}") from None
        return user


def _update_pki_user(db, user, pki_data: PKIUserData):
    """
    Update an existing user's PKI data and certificate metadata.

    Args:
        db: Database session
        user: Existing User object
        pki_data: PKI user data with certificate metadata

    Returns:
        Updated User object
    """
    subject_dn = pki_data["subject_dn"]
    cert_email = pki_data["email"]

    logger.info(f"Updating existing user from PKI: {subject_dn}")

    # Log if certificate email differs from stored email for audit purposes
    # PKI users are identified by DN, not email, so we don't auto-update email
    if cert_email and cert_email != user.email:
        logger.warning(
            f"SECURITY: PKI certificate email differs from stored email. "
            f"subject_dn={subject_dn}, stored_email={user.email}, cert_email={cert_email}. "
            "Email not updated - user identified by DN."
        )

    user.pki_subject_dn = subject_dn
    user.auth_type = AUTH_TYPE_PKI

    # Update certificate metadata if available
    if pki_data.get("serial_number"):
        user.pki_serial_number = pki_data["serial_number"]
    if pki_data.get("issuer_dn"):
        user.pki_issuer_dn = pki_data["issuer_dn"]
    if pki_data.get("organization"):
        user.pki_organization = pki_data["organization"]
    if pki_data.get("organizational_unit"):
        user.pki_organizational_unit = pki_data["organizational_unit"]
    if pki_data["common_name"]:
        user.pki_common_name = pki_data["common_name"]
    not_before_str = pki_data.get("not_before")
    if not_before_str:
        from datetime import datetime

        user.pki_not_before = datetime.fromisoformat(not_before_str)
    not_after_str = pki_data.get("not_after")
    if not_after_str:
        from datetime import datetime

        user.pki_not_after = datetime.fromisoformat(not_after_str)
    fingerprint = pki_data.get("fingerprint")
    if fingerprint:
        # Store fingerprint without colons for consistent storage
        user.pki_fingerprint_sha256 = fingerprint.replace(":", "")

    # Update admin role based on PKI_ADMIN_DNS list
    if pki_data["is_admin"]:
        # External IdPs grant at most 'admin'; never demote a local super_admin.
        if user.role not in ("admin", "super_admin"):
            logger.info(f"Promoting PKI user {subject_dn} to admin")
            user.role = "admin"
        user.is_superuser = user.role == "super_admin"
    elif user.role == "admin":
        # Demote if user was admin but no longer in PKI_ADMIN_DNS
        logger.info(f"Demoting PKI user {subject_dn} from admin (removed from PKI_ADMIN_DNS)")
        user.role = "user"
        user.is_superuser = False

    db.commit()
    return user


def _convert_local_user_to_pki(db, user, pki_data: PKIUserData):
    """
    Convert an existing local user to PKI authentication.

    This is called when a user with auth_type='local' authenticates
    via PKI certificate. The user is converted to PKI auth, which means:
    - auth_type is set to 'pki'
    - hashed_password is cleared (PKI users don't have local passwords)
    - pki_subject_dn is set from the certificate
    - Certificate metadata is stored
    - Admin role is updated based on PKI_ADMIN_DNS

    Args:
        db: Database session
        user: Existing User object with auth_type='local'
        pki_data: PKI user data from certificate

    Returns:
        Updated User object
    """
    subject_dn = pki_data["subject_dn"]

    logger.info(f"Converting local user {user.email} to PKI auth: {subject_dn}")

    # Convert to PKI authentication
    user.auth_type = AUTH_TYPE_PKI
    user.pki_subject_dn = subject_dn
    user.hashed_password = EXTERNAL_AUTH_NO_PASSWORD  # Clear local password

    # Store certificate metadata
    if pki_data.get("serial_number"):
        user.pki_serial_number = pki_data["serial_number"]
    if pki_data.get("issuer_dn"):
        user.pki_issuer_dn = pki_data["issuer_dn"]
    if pki_data.get("organization"):
        user.pki_organization = pki_data["organization"]
    if pki_data.get("organizational_unit"):
        user.pki_organizational_unit = pki_data["organizational_unit"]
    if pki_data["common_name"]:
        user.pki_common_name = pki_data["common_name"]
    not_before_str = pki_data.get("not_before")
    if not_before_str:
        from datetime import datetime

        user.pki_not_before = datetime.fromisoformat(not_before_str)
    not_after_str = pki_data.get("not_after")
    if not_after_str:
        from datetime import datetime

        user.pki_not_after = datetime.fromisoformat(not_after_str)
    fingerprint = pki_data.get("fingerprint")
    if fingerprint:
        # Store fingerprint without colons for consistent storage
        user.pki_fingerprint_sha256 = fingerprint.replace(":", "")

    # Update admin role based on PKI_ADMIN_DNS list
    if pki_data["is_admin"]:
        # External IdPs grant at most 'admin'; never demote a local super_admin.
        if user.role not in ("admin", "super_admin"):
            logger.info(f"Promoting converted PKI user {subject_dn} to admin")
            user.role = "admin"
        user.is_superuser = user.role == "super_admin"
    elif user.role == "admin":
        # Demote if user was admin locally but not in PKI_ADMIN_DNS
        logger.info(f"Demoting converted PKI user {subject_dn} from admin (not in PKI_ADMIN_DNS)")
        user.role = "user"
        user.is_superuser = False

    db.commit()
    return user


def sync_pki_user_to_db(db, pki_data: PKIUserData):
    """
    Create or update a user in the database from PKI certificate data.

    Handles:
    - Creating new users on first PKI login
    - Updating existing PKI users
    - Protecting existing local users from being converted
    - Admin role promotion and demotion based on PKI_ADMIN_DNS
    - Race conditions when multiple concurrent logins occur

    Args:
        db: Database session
        pki_data: User data from certificate

    Returns:
        User: The created or updated User object

    Raises:
        ValueError: If user cannot be created or found after race condition
    """
    from app.auth.constants import AUTH_TYPE_LOCAL
    from app.models.user import User

    subject_dn = pki_data["subject_dn"]
    email = pki_data["email"]

    # Check if user exists by pki_subject_dn first (most specific)
    user = db.query(User).filter(User.pki_subject_dn == subject_dn).first()
    if not user and email:
        user = db.query(User).filter(User.email == email).first()

    if not user:
        user = _create_pki_user(db, pki_data)
    elif user.auth_type == AUTH_TYPE_LOCAL:
        # Convert local users to PKI auth when they authenticate via PKI certificate
        # This ensures they use PKI going forward and cannot change their password
        # (since PKI users don't have local passwords)
        logger.warning(
            f"SECURITY: Converting local user {email} to PKI auth. "
            "User will now authenticate exclusively via PKI certificate. "
            "Local password will be cleared."
        )
        user = _convert_local_user_to_pki(db, user, pki_data)
    else:
        user = _update_pki_user(db, user, pki_data)

    db.refresh(user)
    return user
