"""SSRF egress guard coverage (#284 A0.1/A0.2/A0.10).

`is_safe_url` existed but had exactly ONE caller — the yt-dlp ingest path. The LLM/ASR
"test connection" and "model discovery" endpoints, and the watch-source S3/SMB
connectors, took an arbitrary host from any authenticated user and fetched it with no
validation. With open self-registration that is effectively anonymous reach into the
deployment's private network and cloud instance metadata.

These tests pin the range checks (no DNS needed for literal IPs) and the opt-in escape
hatch for self-hosted Ollama/vLLM and LAN NAS devices.
"""

from __future__ import annotations

import pytest

from app.utils.url_validation import assert_safe_outbound_url
from app.utils.url_validation import is_safe_url
from tests.helpers import does_not_raise


@pytest.mark.parametrize(
    ("url", "expected_reason"),
    [
        # Cloud instance metadata — the highest-value SSRF target.
        ("http://169.254.169.254/latest/meta-data/", "metadata"),
        ("http://169.254.170.2/v2/credentials", "metadata"),
        ("http://[fd00:ec2::254]/latest/meta-data/", "metadata"),
        # IPv6-mapped IPv4 must be judged as its IPv4 form, not as a v6 address.
        ("http://[::ffff:169.254.169.254]/", "metadata"),
        # Loopback / private / link-local.
        ("http://127.0.0.1:8080/", "Loopback"),
        ("http://[::1]/", "Loopback"),
        ("http://10.0.0.5/", "Private"),
        ("http://172.16.0.1/", "Private"),
        ("http://192.168.1.1/", "Private"),
        ("http://[fd00::1]/", "Private"),  # IPv6 ULA
        ("http://[fe80::1]/", "Link-local"),
        ("http://169.254.1.1/", "Link-local"),
        # Gaps that passed every check before #284: unspecified and multicast.
        ("http://0.0.0.0:8080/", "Unspecified"),
        ("http://[::]/", "Unspecified"),
        ("http://224.0.0.1/", "Multicast"),
        # Non-HTTP schemes.
        ("file:///etc/passwd", "HTTP/HTTPS"),
        ("gopher://evil/", "HTTP/HTTPS"),
        ("ftp://internal/", "HTTP/HTTPS"),
        # Malformed input must be refused, not crash.
        ("http://example.com:notaport/", "Invalid port"),
        ("http://", "hostname"),
        ("not-a-url", "HTTP/HTTPS"),
    ],
)
def test_blocks_internal_and_malformed_targets(url, expected_reason):
    safe, reason = is_safe_url(url)
    assert safe is False, f"{url} should be refused"
    assert expected_reason.lower() in reason.lower(), f"{url}: unexpected reason {reason!r}"


@pytest.mark.parametrize(
    "hostname", ["localhost", "localhost.localdomain", "metadata.google.internal"]
)
def test_blocks_known_internal_hostnames(hostname):
    safe, reason = is_safe_url(f"http://{hostname}/")
    assert safe is False
    assert hostname in reason or "Blocked hostname" in reason


def test_allow_private_permits_lan_targets():
    """Self-hosted Ollama/vLLM on a private LAN is a legitimate configuration."""
    for url in ("http://192.168.1.50:11434/", "http://10.1.2.3:8000/v1", "http://127.0.0.1/"):
        safe, reason = is_safe_url(url, allow_private=True)
        assert safe is True, f"{url} should be allowed with allow_private: {reason}"


def test_allow_private_still_rejects_non_http_schemes():
    """The escape hatch loosens the address range, not the scheme allowlist."""
    safe, _ = is_safe_url("file:///etc/passwd", allow_private=True)
    assert safe is False


def test_assert_raises_generic_error_without_leaking_the_reason():
    """SSRF here is semi-blind — the reason must not turn the endpoint into a scanner."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        assert_safe_outbound_url("http://169.254.169.254/", purpose="test")

    assert exc.value.status_code == 400
    detail = str(exc.value.detail).lower()
    for leak in ("metadata", "169.254", "private", "loopback", "reserved"):
        assert leak not in detail, f"rejection reason leaked {leak!r} to the caller"


def test_assert_passes_public_url():
    assert_safe_outbound_url("https://api.openai.com/v1", purpose="test")  # must not raise


def test_multi_record_host_is_rejected_if_any_record_is_private(monkeypatch):
    """A host with one public and one private A record must not pass.

    Checking only the first record leaves the private address reachable on a later
    resolution — the DNS-rebinding shape called out in A0.2.
    """
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    safe, reason = is_safe_url("http://dual-record.example.com/")
    assert safe is False
    assert "metadata" in reason.lower()


def test_resolve_returns_addresses_for_pinning(monkeypatch):
    """Callers get the resolved addresses back so they can pin them."""
    import socket

    from app.utils.url_validation import resolve_public_addresses

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))],
    )

    addresses, reason = resolve_public_addresses("https://example.com/")
    assert reason == ""
    assert addresses == ["93.184.216.34"]


def test_unresolvable_host_is_refused():
    safe, reason = is_safe_url("http://this-host-does-not-exist.invalid/")
    assert safe is False
    assert "resolve" in reason.lower()


# ── Watch-source targets (A0.10) ─────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["http://169.254.169.254/", "http://127.0.0.1:9000"])
def test_s3_watch_source_refuses_internal_endpoint(monkeypatch, value):
    from app.core.config import settings
    from app.services.watch_sources.base import _assert_safe_watch_target

    monkeypatch.setattr(settings, "WATCH_ALLOW_PRIVATE_ENDPOINTS", False)
    with pytest.raises(ValueError, match="publicly reachable"):
        _assert_safe_watch_target(value, source_type="s3")


def test_smb_bare_host_is_validated(monkeypatch):
    """SMB gives a bare hostname, not a URL — it must still be range-checked."""
    from app.core.config import settings
    from app.services.watch_sources.base import _assert_safe_watch_target

    monkeypatch.setattr(settings, "WATCH_ALLOW_PRIVATE_ENDPOINTS", False)
    with pytest.raises(ValueError, match="publicly reachable"):
        _assert_safe_watch_target("169.254.169.254", source_type="smb")


def test_watch_private_targets_allowed_by_default(monkeypatch):
    """A LAN NAS is the normal single-tenant case, so this defaults on."""
    from app.core.config import settings
    from app.services.watch_sources.base import _assert_safe_watch_target

    monkeypatch.setattr(settings, "WATCH_ALLOW_PRIVATE_ENDPOINTS", True)
    _assert_safe_watch_target("192.168.1.10", source_type="smb")  # must not raise
    with does_not_raise("watch sources may target private hosts by default"):
        _assert_safe_watch_target("http://10.0.0.9:9000", source_type="s3")


def test_watch_guard_ignores_empty_target(monkeypatch):
    from app.services.watch_sources.base import _assert_safe_watch_target

    _assert_safe_watch_target(None, source_type="s3")
    with does_not_raise("an empty target is ignored by the guard, not rejected"):
        _assert_safe_watch_target("", source_type="smb")
