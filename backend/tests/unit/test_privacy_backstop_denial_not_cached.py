"""A denial the backstop made because it COULD NOT DECIDE must not be remembered (#1280, D4).

``assert_voiceprint_write_allowed`` caches its answer for a few seconds because it sits on every
OpenSearch write. A clean "no" (a switch really is off) may be cached; a "no" that is only a
failure to read the policy must not be, or one hiccup of the database or of the platform
resolver would refuse every voiceprint write for that scope for the whole window after the
hiccup is over.

The guard is the ``if clean:`` around the cache write. The DDL-based test that dropped a table to
provoke the storage failure is ``ddl_exclusive`` and cannot run beside the other suites; these two
provoke the same two failure paths without touching the schema, so a plain run catches the guard
being removed. Each has the control that makes it falsifiable: a denial from a switch that IS off
is cached, so "never cache anything" cannot pass.
"""

from __future__ import annotations

import pytest

from app.core import constants
from app.services.privacy import policy
from app.services.privacy.policy import PrivacyCeiling
from tests.fixtures.privacy_switches import set_instance_switch


@pytest.fixture
def backstop(db_session):
    policy.reset_privacy_ceiling_resolver()
    policy.clear_write_backstop_cache()
    yield lambda user_id: policy.assert_voiceprint_write_allowed(user_id, None, db=db_session)
    policy.reset_privacy_ceiling_resolver()
    policy.clear_write_backstop_cache()


def test_a_platform_resolver_failure_is_a_denial_that_is_not_cached(
    backstop, normal_user, monkeypatch
):
    def broken(db, organization_id):
        raise RuntimeError("platform layer unreachable")

    # monkeypatch, not set_privacy_ceiling_resolver: that one clears the cache by itself and
    # would hide a denial that was wrongly remembered.
    monkeypatch.setattr(policy, "_ceiling_resolver", broken)
    assert backstop(normal_user.id) is False

    monkeypatch.setattr(policy, "_ceiling_resolver", policy._community_ceiling_resolver)

    assert backstop(normal_user.id) is True


def test_a_storage_read_failure_is_a_denial_that_is_not_cached(backstop, normal_user, monkeypatch):
    real = policy._stored_layers

    def broken(db, user_id, organization_id):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(policy, "_stored_layers", broken)
    assert backstop(normal_user.id) is False

    monkeypatch.setattr(policy, "_stored_layers", real)

    assert backstop(normal_user.id) is True


def test_a_denial_from_a_switch_that_is_really_off_is_cached(
    backstop, db_session, normal_user, monkeypatch
):
    """The control: only an undecidable denial escapes the cache."""
    set_instance_switch(db_session, "voiceprints", False)
    assert backstop(normal_user.id) is False

    set_instance_switch(db_session, "voiceprints", True)
    # A cached clean denial is the documented staleness window ...
    assert backstop(normal_user.id) is False

    # ... which ends when the cache does.
    monkeypatch.setattr(constants, "PRIVACY_WRITE_BACKSTOP_TTL_SECONDS", 0.0)
    policy.clear_write_backstop_cache()
    assert backstop(normal_user.id) is True


def test_a_resolver_answer_of_the_wrong_type_is_a_denial_that_is_not_cached(
    backstop, normal_user, monkeypatch
):
    monkeypatch.setattr(policy, "_ceiling_resolver", lambda db, organization_id: "allow")
    assert backstop(normal_user.id) is False

    monkeypatch.setattr(policy, "_ceiling_resolver", lambda db, organization_id: PrivacyCeiling())

    assert backstop(normal_user.id) is True
