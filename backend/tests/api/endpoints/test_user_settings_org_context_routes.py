"""Behaviour tests for ``/api/user-settings/organization-context*`` (issue #431).

The organisation-context group is the only per-user settings surface whose rows are
readable by *other* accounts: ``is_shared=true`` publishes the text to everyone, and
``use-shared`` adopts someone else's. That makes two things worth pinning that no
test covered:

* **Whose row answers a GET.** ``_build_org_context_response`` filters by
  ``current_user.id``; adopting a shared context stores only a pointer
  (``org_context_use_shared_from``) and must not overwrite the adopter's own text.
* **Un-sharing revokes.** ``PUT {"is_shared": false}`` deletes every *other* user's
  ``org_context_use_shared_from`` row pointing at the un-sharer. Nothing asserted
  that the revocation reaches the other account.

**This router is NOT capability-gated.** ``api/router.py`` mounts ``/user-settings``
with no ``capability=`` argument (unlike ``/llm-settings`` → ``llm.user_settings``),
so an unauthenticated request is **401**, not the 404 that ``require_capability``
produces. Both the default-resolver and ``organizations``-enabled cases are asserted
below so a later gate cannot be added silently.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.core.constants import ORG_CONTEXT_MAX_LENGTH

_BASE = "/api/user-settings/organization-context"

_DEFAULTS = {
    "context_text": "",
    "include_in_default_prompts": True,
    "include_in_custom_prompts": False,
    "is_shared": False,
    "using_shared_from": None,
}

_ROUTES = [
    ("GET", ""),
    ("PUT", ""),
    ("DELETE", ""),
    ("GET", "/shared"),
    ("POST", "/use-shared"),
]


@pytest.mark.parametrize(("method", "path"), _ROUTES, ids=[f"{m} {p or '/'}" for m, p in _ROUTES])
def test_route_requires_authentication(client, method, path):
    """401 — a capability-gated router would answer 404 here instead."""
    resp = client.request(method, f"{_BASE}{path}", json={})
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_reachable_without_the_organizations_capability(client, user_token_headers):
    """Default resolver: no ``organizations`` capability is declared, and the route
    still answers 200 — this surface is per-user, not tenant-scoped."""
    resp = client.get(_BASE, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK


def test_reachable_with_the_organizations_capability_on(
    client, user_token_headers, organizations_capability_on
):
    """Control for the test above: turning the capability on changes nothing, which
    is what "not gated" means."""
    resp = client.get(_BASE, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK


# ===========================================================================
# Own context: round trip, reset, isolation
# ===========================================================================


def test_defaults_are_the_coded_constants(client, user_token_headers):
    resp = client.get(_BASE, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == _DEFAULTS


def test_round_trip_persists_across_requests(client, user_token_headers):
    put = client.put(
        _BASE,
        json={
            "context_text": "Acme Robotics builds warehouse AGVs.",
            "include_in_default_prompts": False,
            "include_in_custom_prompts": True,
        },
        headers=user_token_headers,
    )
    assert put.status_code == status.HTTP_200_OK

    reread = client.get(_BASE, headers=user_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["context_text"] == "Acme Robotics builds warehouse AGVs."
    assert reread.json()["include_in_default_prompts"] is False
    assert reread.json()["include_in_custom_prompts"] is True


def test_delete_reverts_to_defaults(client, user_token_headers):
    put = client.put(_BASE, json={"context_text": "temporary"}, headers=user_token_headers)
    assert put.status_code == status.HTTP_200_OK

    reset = client.delete(_BASE, headers=user_token_headers)
    assert reset.status_code == status.HTTP_200_OK
    assert reset.json()["default_settings"]["context_text"] == ""

    after = client.get(_BASE, headers=user_token_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json() == _DEFAULTS


def test_empty_put_is_a_no_op_that_returns_current_state(client, user_token_headers):
    seed = client.put(_BASE, json={"context_text": "kept"}, headers=user_token_headers)
    assert seed.status_code == status.HTTP_200_OK

    noop = client.put(_BASE, json={}, headers=user_token_headers)
    assert noop.status_code == status.HTTP_200_OK
    assert noop.json()["context_text"] == "kept"


def test_over_length_context_text_is_422(client, user_token_headers):
    resp = client.put(
        _BASE,
        json={"context_text": "x" * (ORG_CONTEXT_MAX_LENGTH + 1)},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_put_does_not_change_other_users_context(
    client, user_token_headers, other_user_auth_headers
):
    before = client.get(_BASE, headers=other_user_auth_headers)
    assert before.status_code == status.HTTP_200_OK
    assert before.json()["context_text"] == ""

    mine = client.put(
        _BASE,
        json={"context_text": "mine only", "include_in_custom_prompts": True},
        headers=user_token_headers,
    )
    assert mine.status_code == status.HTTP_200_OK

    # Positive control: the same GET, as A, DID move — so "B unchanged" cannot be
    # passing merely because the read always answers the coded default.
    mine_reread = client.get(_BASE, headers=user_token_headers)
    assert mine_reread.status_code == status.HTTP_200_OK
    assert mine_reread.json()["context_text"] == "mine only"
    assert mine_reread.json()["include_in_custom_prompts"] is True

    after = client.get(_BASE, headers=other_user_auth_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json() == before.json()


def test_delete_does_not_reset_other_users_context(
    client, user_token_headers, other_user_auth_headers
):
    theirs = client.put(_BASE, json={"context_text": "theirs"}, headers=other_user_auth_headers)
    assert theirs.status_code == status.HTTP_200_OK

    seeded_mine = client.put(_BASE, json={"context_text": "mine"}, headers=user_token_headers)
    assert seeded_mine.status_code == status.HTTP_200_OK

    reset = client.delete(_BASE, headers=user_token_headers)
    assert reset.status_code == status.HTTP_200_OK

    # Positive control: A's own row really was removed by the same call.
    mine_after = client.get(_BASE, headers=user_token_headers)
    assert mine_after.status_code == status.HTTP_200_OK
    assert mine_after.json()["context_text"] == ""

    after = client.get(_BASE, headers=other_user_auth_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json()["context_text"] == "theirs"


# ===========================================================================
# Sharing: /shared listing and /use-shared adoption
# ===========================================================================


def _share(client, headers, text: str):
    resp = client.put(
        _BASE,
        json={"context_text": text, "is_shared": True},
        headers=headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["is_shared"] is True
    return resp


def test_shared_listing_carries_owner_attribution(
    client, user_token_headers, other_user_auth_headers, normal_user
):
    _share(client, user_token_headers, "Corp-wide background")

    listing = client.get(f"{_BASE}/shared", headers=other_user_auth_headers)
    assert listing.status_code == status.HTTP_200_OK
    entries = [c for c in listing.json()["shared_contexts"] if c["user_id"] == str(normal_user.id)]
    assert len(entries) == 1
    assert entries[0]["context_text"] == "Corp-wide background"
    assert entries[0]["owner_name"] == normal_user.full_name
    assert entries[0]["owner_role"] == "user"
    assert entries[0]["is_active"] is False


def test_shared_listing_excludes_the_callers_own_context(client, user_token_headers, normal_user):
    _share(client, user_token_headers, "Only mine")

    listing = client.get(f"{_BASE}/shared", headers=user_token_headers)
    assert listing.status_code == status.HTTP_200_OK
    owners = [c["user_id"] for c in listing.json()["shared_contexts"]]
    assert str(normal_user.id) not in owners


def test_shared_listing_skips_a_sharer_with_no_context_text(
    client, user_token_headers, other_user_auth_headers, normal_user
):
    """``is_shared`` with an empty ``context_text`` publishes nothing — the handler's
    ``if not ctx_text: continue`` branch."""
    flag_only = client.put(_BASE, json={"is_shared": True}, headers=user_token_headers)
    assert flag_only.status_code == status.HTTP_200_OK
    assert flag_only.json()["is_shared"] is True

    listing = client.get(f"{_BASE}/shared", headers=other_user_auth_headers)
    assert listing.status_code == status.HTTP_200_OK
    owners = [c["user_id"] for c in listing.json()["shared_contexts"]]
    assert str(normal_user.id) not in owners


def test_adopting_a_shared_context_marks_it_active_without_overwriting_own_text(
    client, user_token_headers, other_user_auth_headers, normal_user
):
    _share(client, user_token_headers, "Sharer text")
    own = client.put(_BASE, json={"context_text": "adopter own"}, headers=other_user_auth_headers)
    assert own.status_code == status.HTTP_200_OK

    adopt = client.post(
        f"{_BASE}/use-shared",
        json={"user_id": normal_user.id},
        headers=other_user_auth_headers,
    )
    assert adopt.status_code == status.HTTP_200_OK
    assert adopt.json()["using_shared_from"] == str(normal_user.id)
    assert adopt.json()["context_text"] == "adopter own"

    listing = client.get(f"{_BASE}/shared", headers=other_user_auth_headers)
    assert listing.status_code == status.HTTP_200_OK
    entries = [c for c in listing.json()["shared_contexts"] if c["user_id"] == str(normal_user.id)]
    assert len(entries) == 1
    assert entries[0]["is_active"] is True


def test_use_shared_with_null_user_id_stops_using_shared(
    client, user_token_headers, other_user_auth_headers, normal_user
):
    _share(client, user_token_headers, "Sharer text")
    adopt = client.post(
        f"{_BASE}/use-shared", json={"user_id": normal_user.id}, headers=other_user_auth_headers
    )
    assert adopt.status_code == status.HTTP_200_OK
    assert adopt.json()["using_shared_from"] == str(normal_user.id)

    stop = client.post(
        f"{_BASE}/use-shared", json={"user_id": None}, headers=other_user_auth_headers
    )
    assert stop.status_code == status.HTTP_200_OK
    assert stop.json()["using_shared_from"] is None

    reread = client.get(_BASE, headers=other_user_auth_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["using_shared_from"] is None


def test_use_shared_for_a_user_who_never_shared_is_404(
    client, other_user_auth_headers, normal_user
):
    resp = client.post(
        f"{_BASE}/use-shared", json={"user_id": normal_user.id}, headers=other_user_auth_headers
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Shared organization context not found"


def test_unsharing_revokes_the_adopters_pointer(
    client, user_token_headers, other_user_auth_headers, normal_user
):
    """The un-share cleanup reaches the OTHER account's row, not just the owner's."""
    _share(client, user_token_headers, "Temporarily public")
    adopt = client.post(
        f"{_BASE}/use-shared", json={"user_id": normal_user.id}, headers=other_user_auth_headers
    )
    assert adopt.status_code == status.HTTP_200_OK
    assert adopt.json()["using_shared_from"] == str(normal_user.id)

    unshare = client.put(_BASE, json={"is_shared": False}, headers=user_token_headers)
    assert unshare.status_code == status.HTTP_200_OK
    assert unshare.json()["is_shared"] is False

    adopter = client.get(_BASE, headers=other_user_auth_headers)
    assert adopter.status_code == status.HTTP_200_OK
    assert adopter.json()["using_shared_from"] is None
