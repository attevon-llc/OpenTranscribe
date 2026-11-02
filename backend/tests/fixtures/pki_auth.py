"""Helpers shared by the PKI authentication tests (``tests/pki_auth``)."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# Skip marker for unit tests that test internal PKI functions
# Integration tests (TestPKIEndpointIntegration, TestAuthMethodsEndpoint, etc.)
# do NOT use this marker and run unconditionally.
# Runs by DEFAULT. This alias used to be `pytest.mark.skipif(RUN_PKI_TESTS != "true")`,
# which kept 105 PKI tests out of every local run and out of CI. They all pass, and did so on
# the first run once the gate was lifted — `scripts/pki/test-certs/{ca,clients,nginx}` is
# present, so the fixtures the gate implied were missing have been there all along.
# Kept as a no-op mark so the ~80 call sites stay valid and the intent stays greppable
# (issue #431).
skip_pki_unit = pytest.mark.pki


# The revocation plane is DB-backed (issue #498): ``auth/pki_auth.py`` resolves
# ``pki_verify_revocation`` / ``pki_revocation_soft_fail`` / ``pki_ca_cert_path`` /
# ``pki_ocsp_timeout_seconds`` / ``pki_crl_cache_seconds`` through
# ``get_process_auth_settings()`` so a Settings UI save takes effect.
#
# ``patch("app.auth.pki_auth.settings")`` therefore no longer controls them —
# ``auth_settings`` imports ``settings`` from ``app.core.config`` directly, so a
# patched module attribute is simply not read. Tests that kept using it were not
# failing loudly; they were silently asserting against the deployment's own
# ``.env`` values, which is worse. ``set_pki`` publishes into the same
# process-level cache a real save primes.
def set_pki(**values):
    """Publish DB-backed PKI settings for the current test."""
    from app.core.process_auth_settings import publish_process_auth_setting

    for key, value in values.items():
        publish_process_auth_setting(key, value)


def generate_test_certificate(
    common_name: str,
    email: str | None,
    organization: str,
    organizational_unit: str,
    days_valid: int,
    days_before: int = 0,
) -> str:
    """
    Generate a test X.509 certificate.

    Args:
        common_name: Certificate CN
        email: Email address (optional)
        organization: Organization name
        organizational_unit: Organizational unit
        days_valid: Days until expiration (negative = already expired)
        days_before: Days before now that certificate becomes valid (negative = not yet valid)

    Returns:
        PEM-encoded certificate string
    """
    # Generate key pair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    # Build subject
    subject_attrs = [
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, organizational_unit),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
    ]
    if email:
        subject_attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))

    subject = x509.Name(subject_attrs)

    # Calculate validity period
    now = datetime.now(UTC)
    not_valid_before = now - timedelta(days=days_before)
    not_valid_after = now + timedelta(days=days_valid)

    # Build certificate
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)  # Self-signed
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before)
        .not_valid_after(not_valid_after)
    )

    # Add email to SAN if provided
    if email:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name(email)]),
            critical=False,
        )

    # Sign certificate
    certificate = builder.sign(private_key, hashes.SHA256(), default_backend())

    # Return PEM-encoded
    return certificate.public_bytes(serialization.Encoding.PEM).decode("utf-8")
