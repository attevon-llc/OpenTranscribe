"""``redaction.llm_guard.is_local_provider`` — the provider-keyed masking gate.

Owner decision, 2026-08-13 (see ``services/chat/CLAUDE.md``): a LOCAL model never has
excerpt text leave the machine, so masking it before the call costs recall for no
egress benefit; a REMOTE provider still gets masked text, because that call is a real
data-egress event. ``is_local_provider`` is the ONE place that classification happens,
and it must **fail closed** — any ambiguity reads as remote.

Every test here avoids live DNS: an IP-literal ``base_url`` needs no resolution at
all, and the two cases that genuinely go through hostname resolution (a SaaS-shaped
host, and a hostname DNS cannot resolve) monkeypatch
``app.utils.url_validation.resolve_public_addresses`` directly rather than depending
on the sandbox actually having network access — a network-dependent unit test would
be exactly the kind of "test that cannot fail the way it claims to" this repo's
auditors exist to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from app.services.redaction.llm_guard import is_local_provider


@dataclass
class _Config:
    provider: str
    base_url: str | None = None


# --------------------------------------------------------------------------- #
# The required seven-case matrix
# --------------------------------------------------------------------------- #


def test_a_remote_hosted_provider_is_masked():
    """openai/anthropic/etc. are third-party APIs by construction — never local."""
    assert is_local_provider(_Config(provider="openai", base_url=None)) is False
    assert is_local_provider(_Config(provider="anthropic")) is False
    assert is_local_provider(_Config(provider="openrouter")) is False


def test_vllm_is_unmasked():
    """`base_url=None` on its own no longer classifies local. Pass the real

    coded default (`config.py:1080`'s `VLLM_BASE_URL`) so this asserts the
    common case a `vllm` deployment actually has, not a config that could
    never reach a model.
    """
    cfg = _Config(provider="vllm", base_url="http://localhost:8012/v1")
    assert is_local_provider(cfg) is True


def test_ollama_is_unmasked():
    """Same as above, `config.py:1088`'s `OLLAMA_BASE_URL` default."""
    cfg = _Config(provider="ollama", base_url="http://localhost:11434")
    assert is_local_provider(cfg) is True


def test_a_hosted_vllm_endpoint_is_masked():
    """A `vllm`-provider config whose `base_url` names a public SaaS host is

    remote — the gap this lane closes. Must-fire: red against the
    pre-fix code, which trusted `provider == "vllm"` alone.
    """
    with patch(
        "app.utils.url_validation.resolve_public_addresses",
        return_value=(["93.184.216.34"], ""),
    ):
        cfg = _Config(provider="vllm", base_url="https://vllm.some-saas.example/v1")
        assert is_local_provider(cfg) is False


def test_a_hosted_ollama_endpoint_is_masked():
    with patch(
        "app.utils.url_validation.resolve_public_addresses",
        return_value=(["93.184.216.34"], ""),
    ):
        cfg = _Config(provider="ollama", base_url="https://ollama.some-saas.example/v1")
        assert is_local_provider(cfg) is False


def test_a_compose_hosted_vllm_is_still_local():
    """A `vllm` config at a docker-compose service name stays local, and

    without a DNS round trip — mirrors `test_dotless_docker_hostname_is_
    unmasked_without_dns` for the `custom` provider.
    """
    with patch("app.utils.url_validation.resolve_public_addresses") as mocked:
        cfg = _Config(provider="vllm", base_url="http://vllm:8000/v1")
        assert is_local_provider(cfg) is True
        mocked.assert_not_called()


def test_vllm_with_no_base_url_fails_closed():
    """A `vllm` config with no endpoint to reach reads remote — inert (it

    cannot serve a model either way), but no longer a special-cased `True`.
    """
    assert is_local_provider(_Config(provider="vllm", base_url=None)) is False


def test_custom_saas_host_is_masked():
    """A `custom` endpoint naming a public SaaS host is remote, exactly like openai."""
    with patch(
        "app.utils.url_validation.resolve_public_addresses",
        return_value=(["93.184.216.34"], ""),
    ):
        cfg = _Config(provider="custom", base_url="https://api.example.com/v1")
        assert is_local_provider(cfg) is False


def test_custom_rfc1918_host_is_unmasked():
    """An IP literal needs no DNS step — RFC1918 is judged straight from the URL."""
    cfg = _Config(provider="custom", base_url="http://192.168.1.50:8000/v1")
    assert is_local_provider(cfg) is True


def test_ambiguous_dns_failure_is_masked():
    """DNS cannot resolve the host at all — ambiguous, so remote (fail closed)."""
    with patch(
        "app.utils.url_validation.resolve_public_addresses",
        return_value=([], "Cannot resolve hostname: nope.invalid"),
    ):
        cfg = _Config(provider="custom", base_url="https://nope.invalid/v1")
        assert is_local_provider(cfg) is False


def test_force_floor_locked_and_local_still_masks():
    """The floor is read by `redactor._gather`, not this function — but the function

    must still correctly report "local" so the floor has something to override.
    Locking is proven end to end in `test_chat_redactor_provider_locality.py`; this
    is the half that belongs to this module: `is_local_provider` itself carries no
    admin-floor awareness and must not — that decision is `cfg.redact_before_llm_locked`,
    resolved elsewhere.
    """
    cfg = _Config(provider="vllm", base_url="http://localhost:8012/v1")
    assert is_local_provider(cfg) is True


# --------------------------------------------------------------------------- #
# Fail-closed: every ambiguity resolves to False (remote — mask)
# --------------------------------------------------------------------------- #


def test_no_base_url_is_masked():
    assert is_local_provider(_Config(provider="custom", base_url=None)) is False
    assert is_local_provider(_Config(provider="custom", base_url="")) is False


def test_an_unterminated_ipv6_bracket_raises_from_urlparse_and_is_masked():
    """`urlparse` genuinely raises `ValueError` on this input — confirmed by

    direct execution: ``urlparse("http://[::1")`` -> ``ValueError: Invalid
    IPv6 URL``. This is the one input in this file that reaches the
    ``except ValueError`` arm around the ``urlparse`` call in
    `_custom_endpoint_is_local`; `test_unparseable_base_url_is_masked`'s old
    fixture (`"::not a url::"`) does NOT raise — it exits via the empty-
    hostname guard instead (see `test_a_string_that_is_not_a_url_has_no_
    hostname_and_is_masked` below), so this test used to claim coverage it
    never exercised.
    """
    cfg = _Config(provider="custom", base_url="http://[::1")
    assert is_local_provider(cfg) is False


def test_the_unterminated_bracket_really_raises():
    """Guard-on-the-guard: pins the raw `urlparse` behaviour so a future

    Python version making this permissive fails loudly HERE, rather than the
    `except ValueError` arm silently going untested again the way it did
    before this file was corrected.
    """
    with pytest.raises(ValueError, match="Invalid IPv6 URL"):
        urlparse("http://[::1")


def test_a_string_that_is_not_a_url_has_no_hostname_and_is_masked():
    """`urlparse("::not a url::")` does NOT raise — it parses cleanly to a

    hostname-less result, so this exits via the empty-hostname guard
    (`test_no_hostname_in_url_is_masked`'s branch), never the
    `except ValueError` arm above.
    """
    assert is_local_provider(_Config(provider="custom", base_url="::not a url::")) is False


def test_no_hostname_in_url_is_masked():
    assert is_local_provider(_Config(provider="custom", base_url="file:///etc/passwd")) is False


def test_mixed_public_and_private_addresses_is_masked():
    """A hostname split between a private and a genuinely public A record is

    exactly the ambiguity this function refuses to guess about. ``203.0.113.0/24``
    would NOT do here — Python's ``ipaddress.is_private`` classifies the whole
    RFC 5737 documentation range (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24)
    as private, so a docs-range address is not actually public in the sense this
    function cares about; ``8.8.8.8`` (a real, routed, non-private address) is.
    """
    with patch(
        "app.utils.url_validation.resolve_public_addresses",
        return_value=(["10.0.0.5", "8.8.8.8"], ""),
    ):
        cfg = _Config(provider="custom", base_url="https://split.example/v1")
        assert is_local_provider(cfg) is False


def test_metadata_blocked_reason_is_masked():
    """A `reason` on the response (cloud IMDS) is refused outright, never local."""
    with patch(
        "app.utils.url_validation.resolve_public_addresses",
        return_value=([], "Cloud metadata endpoint blocked"),
    ):
        cfg = _Config(provider="custom", base_url="http://metadata.example/v1")
        assert is_local_provider(cfg) is False


# --------------------------------------------------------------------------- #
# IP literals, both families — `_is_local_address`'s ipv4_mapped unwrap and its
# is_loopback/is_link_local/is_private disjunction, previously covered only by
# one IPv4-private case. All IP literals below need no DNS patching.
# --------------------------------------------------------------------------- #


def test_ipv4_loopback_literal_is_unmasked():
    cfg = _Config(provider="custom", base_url="http://127.0.0.1:8000/v1")
    assert is_local_provider(cfg) is True


def test_ipv6_loopback_literal_is_unmasked():
    cfg = _Config(provider="custom", base_url="http://[::1]:8000/v1")
    assert is_local_provider(cfg) is True


def test_ipv6_unique_local_address_is_unmasked():
    """`fd00::/8` — the IPv6 ULA range, read by `is_private`."""
    cfg = _Config(provider="custom", base_url="http://[fd00::1]/v1")
    assert is_local_provider(cfg) is True


def test_ipv6_link_local_is_unmasked():
    cfg = _Config(provider="custom", base_url="http://[fe80::1]/v1")
    assert is_local_provider(cfg) is True


def test_ipv4_link_local_literal_is_unmasked():
    """`169.254.169.254` — the AWS/GCP/Azure metadata address, as a URL

    LITERAL rather than a resolved DNS answer. This never reaches the
    cloud-metadata-refusal path in `url_validation` (that only fires on a
    resolved lookup) — deliberately inert here, since a literal IP is
    classified straight from the address, not from a metadata probe.
    """
    cfg = _Config(provider="custom", base_url="http://169.254.169.254/v1")
    assert is_local_provider(cfg) is True


def test_ipv4_mapped_ipv6_loopback_is_unmasked():
    """`::ffff:127.0.0.1` unwraps to the IPv4 loopback it maps."""
    cfg = _Config(provider="custom", base_url="http://[::ffff:127.0.0.1]/v1")
    assert is_local_provider(cfg) is True


def test_ipv4_mapped_ipv6_public_address_is_masked():
    """Must-fire: proves the ipv4_mapped unwrap DISCRIMINATES rather than

    blanket-approving every `::ffff:*` address.
    """
    cfg = _Config(provider="custom", base_url="http://[::ffff:8.8.8.8]/v1")
    assert is_local_provider(cfg) is False


def test_public_ipv6_literal_is_masked():
    cfg = _Config(provider="custom", base_url="http://[2001:4860:4860::8888]/v1")
    assert is_local_provider(cfg) is False


def test_public_ipv4_literal_is_masked():
    cfg = _Config(provider="custom", base_url="http://8.8.8.8/v1")
    assert is_local_provider(cfg) is False


# Note: `203.0.113.x` (RFC 5737 documentation range) is NOT a good "public"
# fixture here — Python's `ipaddress.is_private` classifies the entire RFC 5737
# range as private (see `test_mixed_public_and_private_addresses_is_masked`'s
# docstring above), so it would misleadingly read as local. Use a real routed
# address like `8.8.8.8` for anything meant to prove "this is public".


def test_unknown_provider_is_masked():
    assert is_local_provider(_Config(provider="bedrock")) is False
    assert is_local_provider(_Config(provider="totally-unknown")) is False


def test_missing_provider_attribute_is_masked():
    """Duck-typed on `.provider`/`.base_url` — an object missing either fails closed."""

    class _Bare:
        pass

    assert is_local_provider(_Bare()) is False


def test_a_none_config_is_masked():
    """D6: no LLM_PROVIDER configured at all is a first-class deployment shape

    (deterministic maps / keyphrase / coverage tiers still run with no `llm`).
    ``getattr(config, "provider", "")`` on ``None`` is ``""``, which is not a
    known local provider, so this needs no special-cased ``None`` branch to
    fail closed — callers reach it via ``getattr(llm, "config", None)``
    (`chat/service.py`), never a bare ``is_local_provider(None)`` in practice,
    but the function itself must not raise either way.
    """
    assert is_local_provider(None) is False


# --------------------------------------------------------------------------- #
# `localhost` and dotless docker-compose hostnames
# --------------------------------------------------------------------------- #


def test_localhost_is_unmasked():
    cfg = _Config(provider="custom", base_url="http://localhost:8000/v1")
    assert is_local_provider(cfg) is True


def test_dotless_docker_hostname_is_unmasked_without_dns():
    """`http://backend:8000` — a docker-compose service name, judged local without

    a DNS round trip (many of the environments this matters in have no resolver
    entry for it at all).
    """
    with patch("app.utils.url_validation.resolve_public_addresses") as mocked:
        cfg = _Config(provider="custom", base_url="http://mock-llm:5199/v1")
        assert is_local_provider(cfg) is True
        mocked.assert_not_called()


def test_provider_is_case_and_whitespace_insensitive():
    cfg = _Config(provider=" VLLM ", base_url="http://localhost:8012/v1")
    assert is_local_provider(cfg) is True
