"""OIDC login state is single-use — the control that stops authorization-code injection.

``OIDCStateStore`` holds the OAuth ``state`` and, with it, the **PKCE code verifier** for
the ten minutes between the authorization redirect and the callback. Before this file no
test called ``store_state``, ``get_state`` or ``delete_state`` at all: ``store_state``
appeared in the whole test tree exactly once, as a *string* in
``tests/api/test_handler_blocking_io.py``'s offload assertion, and the other two appeared
nowhere. A mutation run on ``app/auth/session.py`` therefore measured absence, not
strength (see the note in ``scripts/run-mutation-tests.sh``).

The consequence of losing the deletion in :meth:`OIDCStateStore.get_state` is **replay**:
a state that survives its first redemption can be presented again, so an attacker who
observes or fixes a ``state`` value can re-drive the callback with their own
authorization code against the victim's stored PKCE verifier — authorization-code
injection, which is exactly what ``state`` + PKCE exist to prevent. Losing the *key*
derivation instead (``store_state`` and ``get_state`` disagreeing) silently breaks every
login; losing the exhaustion cap lets an unauthenticated caller grow the store without
limit.

These run against the real :class:`InMemoryStore` — the documented fallback backend — so
no Redis is needed and nothing here is a mock of the thing under test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.auth.session import OIDC_STATE_PREFIX
from app.auth.session import InMemoryStore
from app.auth.session import OIDCStateStore

STATE = "3f8a1c9e-state-value"
OTHER_STATE = "b7d2e4a0-state-value"

#: What the flow actually stores: the PKCE verifier plus where to send the user next.
STATE_DATA = {"code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk", "next": "/gallery"}


@pytest.fixture
def store() -> OIDCStateStore:
    """A state store backed by the real in-memory fallback, not Redis.

    Assigning ``_store`` is the supported seam: the ``store`` property lazy-loads
    ``_get_store()`` only while it is ``None``, and the class documents the backend as
    deliberately structural (the fallback implements just the Redis subset used here).
    """
    instance = OIDCStateStore()
    instance._store = InMemoryStore()
    return instance


def _raw(store: OIDCStateStore, state: str) -> Any:
    """Read the backing store directly, by the key the flow is supposed to use."""
    return store.store.get(f"{OIDC_STATE_PREFIX}{state}")


class TestStateRoundTripsExactlyOnce:
    """Consequence prevented: OIDC state / PKCE-verifier REPLAY."""

    def test_a_stored_state_is_returned_once(self, store):
        assert store.store_state(STATE, STATE_DATA) is True
        assert store.get_state(STATE) == STATE_DATA

    def test_the_pkce_verifier_survives_the_round_trip_intact(self, store):
        """A truncated or re-encoded verifier fails the token exchange, not the test."""
        store.store_state(STATE, STATE_DATA)

        redeemed = store.get_state(STATE)

        assert redeemed is not None
        assert redeemed["code_verifier"] == STATE_DATA["code_verifier"]

    def test_a_second_redemption_returns_nothing(self, store):
        """THE control: the callback may be completed once, and only once."""
        store.store_state(STATE, STATE_DATA)
        store.get_state(STATE)

        assert store.get_state(STATE) is None

    def test_redemption_removes_the_value_from_the_backing_store(self, store):
        """Not merely hidden by the reader — actually gone, so no replica can serve it."""
        store.store_state(STATE, STATE_DATA)
        store.get_state(STATE)

        assert _raw(store, STATE) is None

    def test_an_unknown_state_returns_none_rather_than_raising(self, store):
        """A forged/expired ``state`` must be a clean refusal, never a 500."""
        assert store.get_state("never-issued") is None

    def test_redeeming_one_state_leaves_another_alone(self, store):
        """A single-use deletion that deleted the whole namespace would log everyone out."""
        store.store_state(STATE, STATE_DATA)
        store.store_state(OTHER_STATE, {"code_verifier": "second", "next": "/"})

        store.get_state(STATE)

        assert store.get_state(OTHER_STATE) == {"code_verifier": "second", "next": "/"}


class TestKeyDerivationIsShared:
    """Consequence prevented: ``store_state`` and ``get_state`` disagreeing about the
    key, which makes every OIDC login fail at the callback — and, on Redis, leaves the
    orphaned verifier behind for its full TTL."""

    def test_the_state_is_stored_under_the_documented_prefix(self, store):
        store.store_state(STATE, STATE_DATA)

        assert json.loads(_raw(store, STATE)) == STATE_DATA

    def test_the_prefix_is_what_the_reader_uses(self, store):
        """Written by hand under the prefix, read back through the public API."""
        store.store.set(f"{OIDC_STATE_PREFIX}{STATE}", json.dumps(STATE_DATA), ex=600)

        assert store.get_state(STATE) == STATE_DATA


class TestExplicitDeletionIsIdempotent:
    """Consequence prevented: the error/abort paths that call ``delete_state`` after a
    redemption raising, which would turn a failed login into a 500."""

    def test_deleting_a_live_state_reports_success(self, store):
        store.store_state(STATE, STATE_DATA)

        assert store.delete_state(STATE) is True

    def test_deleting_a_live_state_makes_it_unredeemable(self, store):
        store.store_state(STATE, STATE_DATA)
        store.delete_state(STATE)

        assert store.get_state(STATE) is None

    def test_deleting_an_already_redeemed_state_reports_false(self, store):
        store.store_state(STATE, STATE_DATA)
        store.get_state(STATE)

        assert store.delete_state(STATE) is False

    def test_deleting_an_unknown_state_reports_false(self, store):
        assert store.delete_state("never-issued") is False

    def test_deleting_twice_is_not_an_error(self, store):
        store.store_state(STATE, STATE_DATA)
        store.delete_state(STATE)

        assert store.delete_state(STATE) is False


class TestStateExhaustionIsCapped:
    """Consequence prevented: an unauthenticated caller growing the state store without
    limit by hitting the login route repeatedly (the reason ``MAX_STATES`` exists)."""

    def test_the_store_never_exceeds_its_cap(self):
        capped = OIDCStateStore(max_states=2)
        capped._store = InMemoryStore()

        for n in range(6):
            capped.store_state(f"state-{n}", {"code_verifier": str(n)})

        assert len(capped.store.keys(f"{OIDC_STATE_PREFIX}*")) <= 2

    def test_a_state_stored_under_the_cap_is_still_redeemable(self):
        """The eviction must not make the cap a functional outage for normal traffic."""
        capped = OIDCStateStore(max_states=2)
        capped._store = InMemoryStore()

        capped.store_state(STATE, STATE_DATA)

        assert capped.get_state(STATE) == STATE_DATA
