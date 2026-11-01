"""PKI identity data: the certificate/user records and DN/certificate parsing.

``parse_certificate`` and ``_extract_certificate_metadata`` read an X.509 certificate,
``_parse_dn_components`` / ``_normalize_dn`` read and canonicalise a subject DN, and
``extract_display_name_from_gov_dn`` turns DoD CAC / PIV common names into a display
name. No trust decisions are made here; those live in ``auth/pki_auth.py``.
"""

import logging
from dataclasses import dataclass
from typing import TypedDict

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger(__name__)


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

    # IdP-brokered X.509 / government space-separated format:
    # "LastName FirstName emailusername" (three space-separated tokens)
    # The third token is the email username (no @domain) used for account lookup.
    parts = cn.split()
    if len(parts) == 3:
        last, first, _email_user = parts
        return f"{first.title()} {last.title()}"

    # Default: return CN as-is
    return cn


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
