"""Trusted-header authentication: the trust rules, and that PKI shares them.

The whole feature is one question — *may this peer assert an identity?* — so most of
what is worth pinning is refusals. The positive path is one test; the rest are the
ways a header must NOT become a session.

``# mypy: disable-error-code="arg-type"`` — the request stand-in below is a plain
object with ``.client`` and ``.headers``, which is everything ``header_trust`` and
``assertion`` read. Typing it as ``starlette.Request`` would mean constructing a full
ASGI scope for every case, or casting at ~30 call sites.
"""
# mypy: disable-error-code="arg-type"

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

import pytest

from app.auth.header_trust import header_assertion_permitted
from app.auth.header_trust import header_source_is_trusted
from app.auth.header_trust import immediate_peer_ip
from app.auth.header_trust import ip_in_networks
from app.auth.header_trust import parse_trusted_proxies
from app.auth.header_trust import shared_secret_matches
from app.auth.proxy.assertion import PROXY_GRANTABLE_ROLES
from app.auth.proxy.assertion import domain_admitted
from app.auth.proxy.assertion import extract_proxy_assertion
from app.auth.proxy.config import SHARED_SECRET_HEADER
from app.auth.proxy.config import ProxyConfig

TRUSTED = "10.0.0.7"
UNTRUSTED = "203.0.113.9"


@dataclass
class _Peer:
    host: str


@dataclass
class _Request:
    """The minimum surface the trust check and the assertion reader touch."""

    headers: dict[str, str] = field(default_factory=dict)
    host: str = TRUSTED

    @property
    def client(self):
        return _Peer(self.host)


def _cfg(**overrides) -> ProxyConfig:
    base = {
        "enabled": True,
        "trusted_proxies": "10.0.0.0/8",
        "email_header": "X-Forwarded-Email",
        "name_header": "X-Forwarded-User",
    }
    base.update(overrides)
    return ProxyConfig(**base)


class TestTheAllowlist:
    def test_an_empty_allowlist_trusts_nobody(self):
        """The rule the feature is built on. Not 'warn and continue'."""
        assert header_source_is_trusted(_Request(), []) is False

    def test_a_cidr_entry_matches_inside_the_range(self):
        networks = parse_trusted_proxies("10.0.0.0/8")
        assert ip_in_networks(TRUSTED, networks)
        assert not ip_in_networks(UNTRUSTED, networks)

    def test_a_bare_address_is_widened_to_a_host_route(self):
        assert ip_in_networks("192.168.1.1", parse_trusted_proxies("192.168.1.1"))
        assert not ip_in_networks("192.168.1.2", parse_trusted_proxies("192.168.1.1"))

    def test_a_malformed_entry_is_dropped_not_widened(self):
        networks = parse_trusted_proxies("not-an-ip, 10.0.0.1")
        assert len(networks) == 1

    def test_a_transport_without_a_peer_matches_nothing(self):
        class _NoClient:
            client = None

        assert immediate_peer_ip(_NoClient()) == "unknown"
        assert not ip_in_networks("unknown", parse_trusted_proxies("0.0.0.0/0"))

    def test_nothing_asserted_is_not_a_refusal(self):
        """A request with no identity header falls through to the other methods."""
        assert header_assertion_permitted(
            _Request(host=UNTRUSTED),
            [],
            asserted=False,
            method="proxy",
            setting_name="proxy_trusted_proxies",
        )

    def test_an_assertion_from_an_untrusted_peer_is_refused(self):
        assert not header_assertion_permitted(
            _Request(host=UNTRUSTED),
            parse_trusted_proxies("10.0.0.0/8"),
            asserted=True,
            method="proxy",
            setting_name="proxy_trusted_proxies",
        )


class TestTheSharedSecret:
    def test_no_configured_secret_skips_the_check(self):
        assert shared_secret_matches(None, "")

    def test_a_missing_header_fails_when_a_secret_is_configured(self):
        assert not shared_secret_matches(None, "s3cret")

    def test_only_the_exact_value_matches(self):
        assert shared_secret_matches("s3cret", "s3cret")
        assert not shared_secret_matches("s3cre", "s3cret")
        assert not shared_secret_matches("s3crett", "s3cret")


class TestExtractingAnAssertion:
    def test_a_trusted_peer_with_an_email_header_authenticates(self):
        request = _Request({"X-Forwarded-Email": "Ada@Example.COM", "X-Forwarded-User": "Ada L"})
        assertion = extract_proxy_assertion(request, _cfg())
        assert assertion is not None
        assert assertion.email == "ada@example.com"
        assert assertion.full_name == "Ada L"
        assert assertion.role is None
        assert assertion.groups_asserted is False

    def test_an_untrusted_peer_is_refused_even_with_a_perfect_header(self):
        request = _Request({"X-Forwarded-Email": "ada@example.com"}, host=UNTRUSTED)
        assert extract_proxy_assertion(request, _cfg()) is None

    def test_an_empty_allowlist_refuses_a_local_peer_too(self):
        request = _Request({"X-Forwarded-Email": "ada@example.com"}, host="127.0.0.1")
        assert extract_proxy_assertion(request, _cfg(trusted_proxies="")) is None

    def test_a_wrong_shared_secret_refuses_a_trusted_peer(self):
        request = _Request({"X-Forwarded-Email": "ada@example.com", SHARED_SECRET_HEADER: "wrong"})
        assert extract_proxy_assertion(request, _cfg(shared_secret="right")) is None

    def test_a_correct_shared_secret_authenticates(self):
        request = _Request({"X-Forwarded-Email": "ada@example.com", SHARED_SECRET_HEADER: "right"})
        assertion = extract_proxy_assertion(request, _cfg(shared_secret="right"))
        assert assertion is not None
        assert assertion.email == "ada@example.com"
        assert assertion.role is None, "the shared secret alone must not grant a role"

    def test_a_value_that_is_not_an_address_is_refused(self):
        request = _Request({"X-Forwarded-Email": "ada"})
        assert extract_proxy_assertion(request, _cfg()) is None

    def test_no_header_at_all_returns_none_without_refusing(self):
        assert extract_proxy_assertion(_Request(), _cfg()) is None


class TestDomainAdmission:
    def test_an_empty_allowlist_admits_everyone(self):
        """Upgrade-safe, and the same reading ``oidc_allowed_groups`` uses."""
        assert domain_admitted("ada@anywhere.test", "")

    def test_a_listed_domain_is_admitted(self):
        assert domain_admitted("ada@example.com", "example.com, other.test")

    def test_an_unlisted_domain_is_refused(self):
        assert not domain_admitted("ada@evil.test", "example.com")

    def test_the_refusal_reaches_the_extractor(self):
        request = _Request({"X-Forwarded-Email": "ada@evil.test"})
        assert extract_proxy_assertion(request, _cfg(allowed_domains="example.com")) is None


class TestTheRoleHeader:
    def test_it_is_off_unless_configured(self):
        """No ``proxy_role_header`` means the proxy grants no privilege at all."""
        request = _Request({"X-Forwarded-Email": "ada@example.com", "X-Role": "admin"})
        assertion = extract_proxy_assertion(request, _cfg())
        assert assertion is not None
        assert assertion.role is None
        assert assertion.is_admin is False

    def test_admin_is_honoured_when_configured(self):
        request = _Request({"X-Forwarded-Email": "ada@example.com", "X-Role": "admin"})
        assertion = extract_proxy_assertion(request, _cfg(role_header="X-Role"))
        assert assertion is not None
        assert assertion.is_admin is True

    def test_super_admin_is_unreachable(self):
        """The cap. A proxy header must never be able to mint the break-glass role."""
        assert "super_admin" not in PROXY_GRANTABLE_ROLES
        request = _Request({"X-Forwarded-Email": "ada@example.com", "X-Role": "super_admin"})
        assertion = extract_proxy_assertion(request, _cfg(role_header="X-Role"))
        assert assertion is not None
        assert assertion.role is None
        assert assertion.is_admin is False

    @pytest.mark.parametrize("value", ["administrator", "root", "owner", "", "  "])
    def test_an_unrecognised_value_grants_nothing(self, value):
        request = _Request({"X-Forwarded-Email": "ada@example.com", "X-Role": value})
        assertion = extract_proxy_assertion(request, _cfg(role_header="X-Role"))
        assert assertion is not None
        assert assertion.role is None


class TestTheGroupsHeader:
    def test_absent_and_empty_are_different_instructions(self):
        """Absent = 'I do not manage your groups'; empty = 'you are in none'."""
        cfg = _cfg(groups_header="X-Groups")
        absent = extract_proxy_assertion(_Request({"X-Forwarded-Email": "a@example.com"}), cfg)
        empty = extract_proxy_assertion(
            _Request({"X-Forwarded-Email": "a@example.com", "X-Groups": ""}), cfg
        )
        assert absent is not None and empty is not None
        assert absent.groups_asserted is False
        assert empty.groups_asserted is True
        assert empty.groups == ()

    def test_values_split_on_the_configured_separator(self):
        cfg = _cfg(groups_header="X-Groups", groups_separator=";")
        request = _Request(
            {"X-Forwarded-Email": "a@example.com", "X-Groups": "CN=Legal,OU=G; CN=Ops,OU=G"}
        )
        assertion = extract_proxy_assertion(request, cfg)
        assert assertion is not None
        assert assertion.groups == ("CN=Legal,OU=G", "CN=Ops,OU=G")

    def test_no_groups_header_configured_means_no_assertion(self):
        request = _Request({"X-Forwarded-Email": "a@example.com", "X-Groups": "admins"})
        assertion = extract_proxy_assertion(request, _cfg())
        assert assertion is not None
        assert assertion.groups_asserted is False


class TestPKISharesTheOneImplementation:
    """The repo's "delete the old one" rule, pinned.

    ``pki_auth`` must not carry a second copy of the trust machinery; its ``_pki_*``
    names are bindings onto ``header_trust``.
    """

    def test_the_pki_helpers_are_the_shared_functions(self):
        from app.auth import pki_auth

        assert pki_auth._get_pki_client_ip is immediate_peer_ip
        assert pki_auth._is_pki_trusted_proxy is ip_in_networks

    def test_pki_header_trust_delegates(self, monkeypatch):
        from app.auth import pki_auth

        monkeypatch.setattr(pki_auth, "_pki_trusted_proxy_networks", [])
        assert pki_auth._pki_header_source_is_trusted(_Request()) is False

        monkeypatch.setattr(
            pki_auth, "_pki_trusted_proxy_networks", parse_trusted_proxies("10.0.0.0/8")
        )
        assert pki_auth._pki_header_source_is_trusted(_Request()) is True
        assert pki_auth._pki_header_source_is_trusted(_Request(host=UNTRUSTED)) is False

    def test_pki_still_fails_closed_with_no_allowlist(self, monkeypatch):
        from app.auth import pki_auth
        from app.core.config import settings

        monkeypatch.setattr(pki_auth, "_pki_trusted_proxy_networks", [])
        request = _Request({settings.PKI_CERT_DN_HEADER: "CN=Ada,O=Corp"}, host="127.0.0.1")
        assert pki_auth._validate_pki_headers_source(request) is False

    def test_pki_does_not_refuse_a_request_that_asserts_nothing(self, monkeypatch):
        from app.auth import pki_auth

        monkeypatch.setattr(pki_auth, "_pki_trusted_proxy_networks", [])
        assert pki_auth._validate_pki_headers_source(_Request()) is True

    def test_no_second_copy_of_the_parser_survives(self):
        """A grep-style guard: the old bodies imported ipaddress inside pki_auth."""
        import inspect

        from app.auth import pki_auth

        source = inspect.getsource(pki_auth)
        assert "import ipaddress" not in source, (
            "pki_auth grew its own address parsing again — the trust check lives in "
            "auth/header_trust.py and has exactly two callers."
        )
