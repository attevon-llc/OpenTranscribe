"""Functional tests for ``GET /audit/{category}`` and ``POST /{category}/test``.

Both live in ``auth_config.py`` under ``/api/admin/auth-config``.

``GET /audit/{category}`` had **no coverage of any kind** — no test in the tree
issued a request to it. That matters more than a missing happy path: the route's
own docstring records that it once had no category check at all, and
``get_audit_log`` skipped its filter for an unrecognised category, so
``/audit/anything`` returned the ENTIRE audit log for every category at once.
The 400 test below is that regression's only guard, and the cross-category test
is the only thing that proves the filter is applied rather than merely present.

``POST /{category}/test`` is partly covered by
``api/test_auth_config_endpoints.py`` (403 for a plain user, 400 for an
unsupported category, the LDAP "no server" envelope) and
``unit/test_oidc_discovery.py`` / ``unit/test_oidc_test_connection.py`` at the
function level. What was missing over HTTP: the anonymous 401, the plain-``admin``
403, and the OIDC branch's own short-circuit. Only the branches that return
*before* any socket is opened are exercised — no test here performs outbound
network I/O, deliberately: this handler fetches a URL an operator typed.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.services.auth_config_service import AuthConfigService

BASE = "/api/admin/auth-config"

#: Two real categories that share no keys, so a row filed under one must not
#: appear under the other.
CATEGORY = "mfa"
OTHER_CATEGORY = "session"
#: A key of ``CATEGORY`` that is not sensitive, so the audit row carries the
#: literal value rather than a mask.
CHANGED_KEY = "mfa_issuer_name"


def test_the_two_categories_used_here_really_are_disjoint():
    """Guard the guard: the cross-category test is vacuous if they overlap.

    ``CONFIG_CATEGORIES`` is derived from the per-category Pydantic schemas, so a
    future refactor could move ``mfa_issuer_name`` into a shared base model and
    silently make the filter assertion below untestable.
    """
    mfa_keys = set(AuthConfigService.CONFIG_CATEGORIES[CATEGORY])
    other_keys = set(AuthConfigService.CONFIG_CATEGORIES[OTHER_CATEGORY])
    assert CHANGED_KEY in mfa_keys
    assert mfa_keys & other_keys == set()


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------
_ROUTES: list[tuple[str, str, dict | None]] = [
    ("GET", f"{BASE}/audit/{CATEGORY}", None),
    ("POST", f"{BASE}/ldap/test", {}),
]


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_a_plain_admin_is_refused_on_every_route(client, admin_token_headers, method, path, body):
    """The audit log names who changed which credential; the test button dials out.

    Catches either being re-gated to ``get_current_admin_user``. The audit read is
    the more subtle of the two: it exposes ``old_value``/``new_value`` for every
    non-sensitive auth key plus the email of the admin who set it.
    """
    response = client.request(method, path, headers=admin_token_headers, json=body)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_a_plain_user_is_refused_on_every_route(client, user_token_headers, method, path, body):
    response = client.request(method, path, headers=user_token_headers, json=body)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_every_route_requires_authentication(client, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /audit/{category}
# ---------------------------------------------------------------------------
def _change_a_setting(client, headers, value: str) -> None:
    """Write one auth-config value, which produces exactly one audit row."""
    response = client.put(f"{BASE}/{CATEGORY}", headers=headers, json={CHANGED_KEY: value})
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["updated_keys"] == [CHANGED_KEY]


def test_audit_records_the_change_with_its_value_and_its_author(
    client, super_admin_token_headers, super_admin_user
):
    """A configuration change is attributable to a person and a value.

    Catches the actor-resolution query being dropped (the entry would render with
    no author, which is the one field that makes an audit log an audit log) and the
    value projection breaking. Both were plausible: ``changed_by`` became nullable
    in v387, so the "unknown author" branch is now genuinely reachable and an
    over-eager fix could route every row through it.
    """
    _change_a_setting(client, super_admin_token_headers, "OT-Audit-Probe")

    response = client.get(f"{BASE}/audit/{CATEGORY}", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    entries = response.json()
    mine = [e for e in entries if e["new_value"] == "OT-Audit-Probe"]
    assert len(mine) == 1
    entry = mine[0]
    assert entry["config_key"] == CHANGED_KEY
    assert entry["changed_by_email"] == super_admin_user.email
    assert entry["change_type"] in {"create", "update"}
    assert entry["created_at"] is not None


def test_audit_does_not_leak_a_change_into_a_sibling_category(client, super_admin_token_headers):
    """The filter is applied, not merely written.

    This is the whole point of the route's category parameter. An implementation
    that ignored it would satisfy the test above — the entry it looks for would
    still be in the response — while handing every caller the full deployment-wide
    change history of every auth method in one request.
    """
    _change_a_setting(client, super_admin_token_headers, "OT-Audit-Isolation")

    response = client.get(f"{BASE}/audit/{OTHER_CATEGORY}", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    values = [e["new_value"] for e in response.json()]
    assert "OT-Audit-Isolation" not in values


def test_audit_for_an_unknown_category_is_a_400_not_the_whole_log(
    client, super_admin_token_headers
):
    """The regression guard: an unrecognised category must be refused.

    ``get_audit_log``'s empty-key-list fall-through turned any unknown segment into
    "no filter", so ``/audit/anything`` answered 200 with every category's entries.
    A 400 is the only response that cannot be mistaken for a legitimate empty page.
    """
    response = client.get(f"{BASE}/audit/not-a-category", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"].startswith("Invalid category. Must be one of:")


def test_audit_limit_is_bounded_at_the_wire(client, super_admin_token_headers):
    """``limit`` is a ``Query(..., le=MAX_AUDIT_LOG_LIMIT)``, so an oversized ask is a 422.

    Catches the bound being moved into the service alone: the endpoint would then
    accept ``limit=1000000`` and the clamp would be invisible to the caller, who
    has no way to tell a truncated page from a complete one.
    """
    response = client.get(
        f"{BASE}/audit/{CATEGORY}",
        params={"limit": 10**9},
        headers=super_admin_token_headers,
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_audit_respects_the_page_size(client, super_admin_token_headers):
    """``limit`` really pages: two changes, one row asked for, one row returned."""
    _change_a_setting(client, super_admin_token_headers, "OT-Audit-Page-1")
    _change_a_setting(client, super_admin_token_headers, "OT-Audit-Page-2")

    response = client.get(
        f"{BASE}/audit/{CATEGORY}", params={"limit": 1}, headers=super_admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()) == 1


# ---------------------------------------------------------------------------
# POST /{category}/test
# ---------------------------------------------------------------------------
def test_oidc_test_without_a_url_is_a_failed_envelope_and_no_request(
    client, super_admin_token_headers
):
    """The OIDC branch refuses to guess a URL, and reports it as data.

    Nothing is dialled: the handler returns before ``httpx`` is constructed, which
    is what makes this assertion safe to run anywhere. Catches the branch being
    replaced by a raise (the panel renders ``message`` inline, so a 500 leaves the
    operator with no diagnosis) or by a default realm URL built from nothing, which
    would send an outbound request the operator never configured.
    """
    response = client.post(f"{BASE}/oidc/test", headers=super_admin_token_headers, json={})
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["success"] is False
    assert body["message"] == "Provide either a discovery URL or a server URL"


def test_oidc_test_refuses_a_blocked_outbound_target(client, super_admin_token_headers):
    """The SSRF guard answers before any socket is opened.

    ``169.254.169.254`` is cloud instance metadata — the canonical SSRF target, and
    the reason ``assert_safe_outbound_url`` was added to a handler that fetches a
    super_admin-supplied URL. Catches the guard being removed: this endpoint would
    become a credential-reading proxy for anyone who reached the super_admin tier.
    """
    response = client.post(
        f"{BASE}/oidc/test",
        headers=super_admin_token_headers,
        json={"oidc_discovery_url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["success"] is False
    assert body["message"] == "That URL is not an allowed outbound target."


def test_a_category_with_no_connection_test_is_a_400(client, super_admin_token_headers):
    """``password_policy`` is a real category with nothing to dial.

    The sibling test in ``api/test_auth_config_endpoints.py`` uses ``mfa``; this
    pins that the refusal is about *testability*, not about the category being
    unknown — an unknown one would be indistinguishable otherwise.
    """
    response = client.post(
        f"{BASE}/password_policy/test", headers=super_admin_token_headers, json={}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == (
        "Connection test not supported for category: password_policy"
    )
