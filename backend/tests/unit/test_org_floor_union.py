"""The organization redaction floor is STORED in core and unioned with a registered resolver.

Before #1279 the floor existed only as a seam: a derived deployment's resolver was the single
source, and core had no storage at all. ``resolve_redaction_floor`` now reads the
``organization_policy`` row and unions it with whatever the resolver returns. These cases run
against the real database (savepoint-isolated), so the read itself is exercised and not a stand-in.

What would silently go wrong, and is pinned here: the stored floor ignored when a resolver is
registered (a deployment's resolver "dropping" every floor an org admin saved); the resolver
ignored once a floor is stored; personal scope reading an organization row; a database error
reading as "no floor" and therefore unmasked text; custom words with no category, so a user who
switched redaction off never has them masked.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.tenant_limits import RedactionFloor
from app.core.tenant_limits import reset_resolvers
from app.core.tenant_limits import resolve_redaction_floor
from app.core.tenant_limits import set_redaction_floor_resolver
from app.models.organization import Organization
from app.models.organization_policy import OrganizationPolicy
from app.services.redaction.config import resolve_effective_config


@pytest.fixture(autouse=True)
def _reset():
    reset_resolvers()
    yield
    reset_resolvers()


def _org(db) -> Organization:
    org = Organization(
        external_org_id=f"org_floor_{uuid.uuid4().hex[:8]}", name="floor", is_active=True
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _store(db, org: Organization, **columns) -> None:
    db.add(OrganizationPolicy(organization_id=org.id, **columns))
    db.commit()


def test_no_row_and_an_all_default_row_both_add_nothing(db_session):
    org = _org(db_session)
    assert resolve_redaction_floor(db_session, org.id) is None
    _store(db_session, org)
    assert resolve_redaction_floor(db_session, org.id) is None


def test_the_stored_floor_is_read_into_a_redaction_floor(db_session):
    org = _org(db_session)
    _store(
        db_session,
        org,
        floor_forced_categories=["pii"],
        floor_forced_pii_entities=["EMAIL"],
        floor_toxicity_threshold=0.3,
        floor_export_redacted=True,
        floor_redact_before_llm=True,
    )
    floor = resolve_redaction_floor(db_session, org.id)
    assert floor == RedactionFloor(
        forced_categories=frozenset({"pii"}),
        forced_pii_entities=frozenset({"EMAIL"}),
        force_toxicity_threshold=0.3,
        force_export_redacted=True,
        force_redact_before_llm=True,
    )


def test_custom_words_force_the_custom_category(db_session):
    """Without it a user who switched redaction off would never have the words masked."""
    org = _org(db_session)
    _store(db_session, org, floor_forced_custom_words=["Project Falcon"])
    floor = resolve_redaction_floor(db_session, org.id)
    assert floor is not None
    assert floor.forced_custom_words == ("Project Falcon",)
    assert "custom" in floor.forced_categories


def test_the_stored_floor_and_a_registered_resolver_both_apply(db_session):
    org = _org(db_session)
    _store(
        db_session,
        org,
        floor_forced_categories=["pii"],
        floor_forced_custom_words=["alpha"],
        floor_toxicity_threshold=0.4,
    )
    set_redaction_floor_resolver(
        lambda db, org_id: RedactionFloor(
            forced_categories=frozenset({"toxicity"}),
            forced_custom_words=("Alpha", "beta"),
            force_toxicity_threshold=0.2,
            force_redact_before_llm=True,
        )
    )
    floor = resolve_redaction_floor(db_session, org.id)
    assert floor is not None
    assert floor.forced_categories == frozenset({"pii", "toxicity", "custom"})
    # case-insensitive de-duplication keeps the first spelling
    assert floor.forced_custom_words == ("alpha", "beta")
    assert floor.force_toxicity_threshold == 0.2  # the more sensitive of the two
    assert floor.force_redact_before_llm is True


def test_a_broken_resolver_does_not_lose_the_stored_floor(db_session):
    org = _org(db_session)
    _store(db_session, org, floor_forced_categories=["pii"])

    def boom(db, org_id):
        raise RuntimeError("resolver exploded")

    set_redaction_floor_resolver(boom)
    floor = resolve_redaction_floor(db_session, org.id)
    assert floor is not None and floor.forced_categories == frozenset({"pii"})


def test_another_organizations_floor_never_applies(db_session):
    mine, theirs = _org(db_session), _org(db_session)
    _store(db_session, theirs, floor_forced_categories=["pii"])
    assert resolve_redaction_floor(db_session, mine.id) is None


def test_personal_scope_does_not_read_an_organization_row(db_session, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("personal scope must not query the organization policy table")

    monkeypatch.setattr(db_session, "query", boom)
    assert resolve_redaction_floor(db_session, None) is None


def test_an_unreadable_stored_floor_raises_instead_of_reading_as_no_floor(db_session, monkeypatch):
    org = _org(db_session)

    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(db_session, "query", boom)
    with pytest.raises(RuntimeError):
        resolve_redaction_floor(db_session, org.id)


def test_the_effective_config_masks_the_orgs_words_for_a_user_who_switched_redaction_off(
    db_session, normal_user
):
    from app import models

    org = _org(db_session)
    _store(db_session, org, floor_forced_custom_words=["acquisition"])
    db_session.add(
        models.UserSetting(
            user_id=normal_user.id, setting_key="redaction_enabled", setting_value="false"
        )
    )
    db_session.commit()

    inside = resolve_effective_config(db_session, normal_user.id, organization_id=org.id)
    outside = resolve_effective_config(db_session, normal_user.id, organization_id=None)

    assert inside.enabled is True
    assert "custom" in inside.enabled_categories
    assert "acquisition" in inside.custom_words
    assert outside.enabled is False and "acquisition" not in outside.custom_words
