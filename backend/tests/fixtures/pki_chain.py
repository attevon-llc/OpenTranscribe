"""A real, throwaway X.509 chain for the PKI revocation and identity tests.

Builds a CA, a leaf it issued (with OCSP and CRL distribution-point URLs), an unrelated
"rogue" CA, and signed OCSP responses / CRLs from any of them, so the signature checks in
``auth/pki_ocsp.py`` and ``auth/pki_revocation.py`` run against genuine cryptography rather
than mocks of it. Nothing touches the network or disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509 import ocsp
from cryptography.x509.oid import AuthorityInformationAccessOID
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.oid import NameOID

OCSP_URL = "http://ocsp.pki-chain.test"
CRL_URLS = ("http://crl-a.pki-chain.test/ca.crl", "http://crl-b.pki-chain.test/ca.crl")

PrivateKey = rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey


def _now() -> datetime:
    return datetime.now(UTC)


def new_key(kind: str = "rsa") -> PrivateKey:
    """A fresh private key: ``rsa`` (2048), ``ec`` (P-256) or ``ed25519``."""
    if kind == "rsa":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if kind == "ec":
        return ec.generate_private_key(ec.SECP256R1())
    return ed25519.Ed25519PrivateKey.generate()


def _sign_alg(key: PrivateKey) -> hashes.SHA256 | None:
    return None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()


def _name(cn: str, org: str = "PKI Chain Test") -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        ]
    )


def make_ca(cn: str = "Chain Test CA", kind: str = "rsa") -> tuple[x509.Certificate, PrivateKey]:
    """A self-signed CA certificate and its key."""
    key = new_key(kind)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - timedelta(days=1))
        .not_valid_after(_now() + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, _sign_alg(key))
    )
    return cert, key


def issue(
    ca_cert: x509.Certificate,
    ca_key: PrivateKey,
    cn: str = "Leaf User",
    *,
    email_in_san: str | None = None,
    crl_urls: tuple[str, ...] = CRL_URLS,
    ocsp_signing: bool = False,
    days_before: int = 1,
    days_valid: int = 365,
) -> tuple[x509.Certificate, PrivateKey]:
    """A certificate issued by ``ca_cert`` carrying the AIA OCSP URL and CRL DPs."""
    key = new_key("rsa")
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn, org="Leaf Org"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - timedelta(days=days_before))
        .not_valid_after(_now() + timedelta(days=days_valid))
        .add_extension(
            x509.AuthorityInformationAccess(
                [
                    x509.AccessDescription(
                        AuthorityInformationAccessOID.OCSP, x509.UniformResourceIdentifier(OCSP_URL)
                    )
                ]
            ),
            critical=False,
        )
    )
    if crl_urls:
        builder = builder.add_extension(
            x509.CRLDistributionPoints(
                [
                    x509.DistributionPoint(
                        full_name=[x509.UniformResourceIdentifier(url)],
                        relative_name=None,
                        reasons=None,
                        crl_issuer=None,
                    )
                    for url in crl_urls
                ]
            ),
            critical=False,
        )
    if email_in_san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name(email_in_san)]), critical=False
        )
    if ocsp_signing:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.OCSP_SIGNING]), critical=False
        )
    return builder.sign(ca_key, _sign_alg(ca_key)), key


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@dataclass(frozen=True)
class Chain:
    """A CA, a leaf it issued, and an unrelated CA that must never be trusted."""

    ca_cert: x509.Certificate
    ca_key: PrivateKey
    leaf_cert: x509.Certificate
    rogue_cert: x509.Certificate
    rogue_key: PrivateKey


def make_chain(kind: str = "rsa") -> Chain:
    ca_cert, ca_key = make_ca(kind=kind)
    leaf, _ = issue(ca_cert, ca_key)
    rogue_cert, rogue_key = make_ca("Rogue CA", kind="rsa")
    return Chain(ca_cert, ca_key, leaf, rogue_cert, rogue_key)


def ocsp_response(
    chain: Chain,
    status: ocsp.OCSPCertStatus,
    *,
    signer_key: PrivateKey | None = None,
    responder_cert: x509.Certificate | None = None,
    embed: tuple[x509.Certificate, ...] = (),
    next_update: timedelta | None = timedelta(hours=1),
) -> bytes:
    """A DER OCSP response about ``chain.leaf_cert``.

    Signed by ``signer_key`` (default: the CA key); ``responder_cert`` names the
    responder in the responder ID (default: the CA); ``embed`` is the certificate list
    a delegated responder ships in the response.
    """
    key = signer_key or chain.ca_key
    this_update = _now() - timedelta(minutes=1)
    builder = ocsp.OCSPResponseBuilder().add_response(
        cert=chain.leaf_cert,
        issuer=chain.ca_cert,
        algorithm=hashes.SHA256(),
        cert_status=status,
        this_update=this_update,
        next_update=this_update + next_update if next_update else None,
        revocation_time=this_update if status == ocsp.OCSPCertStatus.REVOKED else None,
        revocation_reason=None,
    )
    builder = builder.responder_id(ocsp.OCSPResponderEncoding.HASH, responder_cert or chain.ca_cert)
    if embed:
        builder = builder.certificates(list(embed))
    return builder.sign(key, _sign_alg(key)).public_bytes(serialization.Encoding.DER)


def crl(
    issuer: x509.Certificate,
    key: PrivateKey,
    revoked_serials: tuple[int, ...] = (),
) -> bytes:
    """A DER CRL from ``issuer``'s name, signed by ``key``, listing ``revoked_serials``."""
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(issuer.subject)
        .last_update(_now() - timedelta(minutes=1))
        .next_update(_now() + timedelta(days=1))
    )
    for serial in revoked_serials:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(serial)
            .revocation_date(_now() - timedelta(minutes=1))
            .build()
        )
    return builder.sign(key, _sign_alg(key)).public_bytes(serialization.Encoding.DER)
