"""Characterization of ``resolve_or_create_tags`` (the batched resolver used by uploads).

Written before the tags backend rewrite. ``pins_current_behaviour`` names pin the
exact-name fallback's limits: it repairs only an *empty* stored normalization, and a
differently-cased spelling of a row whose stored normalization is wrong creates a duplicate.
"""

from __future__ import annotations

from typing import Any

import pytest
from fixtures.tag_world import build_org_world
from fixtures.tag_world import make_tag
from fixtures.tag_world import word

from app.models.tag import Tag
from app.services.tag_service import resolve_or_create_tags


@pytest.fixture
def commits(monkeypatch, db_session) -> list[int]:
    seen: list[int] = []
    original = db_session.commit

    def counting():
        seen.append(1)
        return original()

    monkeypatch.setattr(db_session, "commit", counting)
    return seen


def _resolve(db, user, names: list[Any], **kwargs):
    return resolve_or_create_tags(
        db, names, user_id=user.id, organization_id=kwargs.pop("organization_id", None), **kwargs
    )


def test_collapsed_variants_keep_the_first_spelling(db_session, normal_user):
    stem = word("Mixed")

    resolved = _resolve(
        db_session, normal_user, [stem.replace("-", " "), stem.upper(), stem.replace("-", "_")]
    )

    assert [t.name for t in resolved] == [stem.replace("-", " ")]
    assert db_session.query(Tag).filter(Tag.user_id == normal_user.id).count() == 1


def test_results_follow_first_seen_order_of_the_normalized_form(db_session, normal_user):
    b, a = word("bbb"), word("aaa")

    resolved = _resolve(db_session, normal_user, [b, a, b.upper()])

    assert [t.name for t in resolved] == [b, a]


def test_blank_none_and_dash_names_are_dropped_silently(db_session, normal_user):
    good = word("good")

    resolved = _resolve(db_session, normal_user, ["", None, "  ", "-", "__", good])

    assert [t.name for t in resolved] == [good]


def test_nothing_usable_returns_an_empty_list_and_adds_nothing(db_session, normal_user):
    before = db_session.query(Tag).count()

    assert _resolve(db_session, normal_user, ["", "-", None]) == []
    assert _resolve(db_session, normal_user, []) == []
    assert db_session.query(Tag).count() == before


def test_a_row_with_a_null_normalization_is_found_by_exact_name_and_repaired(
    db_session, normal_user
):
    legacy = Tag(name="Legacy Name", user_id=normal_user.id, source="manual", normalized_name=None)
    db_session.add(legacy)
    db_session.flush()

    (resolved,) = _resolve(db_session, normal_user, ["Legacy Name"])

    assert resolved.id == legacy.id
    assert resolved.normalized_name == "legacy name"


def test_a_row_with_a_wrong_normalization_is_found_by_exact_name_but_not_repaired_pins_current_behaviour(
    db_session, normal_user
):
    stale = make_tag(db_session, "Weekly Sync", user_id=normal_user.id)
    stale.normalized_name = "stale"
    db_session.flush()
    before = db_session.query(Tag).filter(Tag.user_id == normal_user.id).count()

    (resolved,) = _resolve(db_session, normal_user, ["Weekly Sync"])

    assert resolved.id == stale.id
    assert db_session.query(Tag).filter(Tag.user_id == normal_user.id).count() == before
    assert resolved.normalized_name == "stale"


def test_a_differently_cased_spelling_of_a_wrong_normalization_row_forks_a_duplicate_pins_current_behaviour(
    db_session, normal_user
):
    """The fallback matches the stored name exactly, so ``weekly sync`` misses ``Weekly Sync``."""
    stale = make_tag(db_session, "Weekly Sync", user_id=normal_user.id)
    stale.normalized_name = "stale"
    db_session.flush()

    (resolved,) = _resolve(db_session, normal_user, ["weekly sync"])

    assert resolved.id != stale.id
    assert resolved.name == "weekly sync"
    assert resolved.normalized_name == "weekly sync"


def test_an_own_row_beats_a_same_named_system_row(db_session, normal_user):
    name = word("pref")
    make_tag(db_session, name, user_id=None)
    mine = make_tag(db_session, name, user_id=normal_user.id)

    (resolved,) = _resolve(db_session, normal_user, [name])

    assert resolved.id == mine.id


def test_a_system_row_is_reused_rather_than_forked(db_session, normal_user):
    system = make_tag(db_session, word("shared"), user_id=None)

    (resolved,) = _resolve(db_session, normal_user, [system.name.upper()])

    assert resolved.id == system.id


def test_new_rows_belong_to_the_given_tenant_and_carry_the_source(db_session):
    world = build_org_world(db_session)

    (resolved,) = resolve_or_create_tags(
        db_session,
        [word("fresh")],
        user_id=world.bob.id,
        organization_id=world.org_a.id,
        source="auto_ai",
    )

    assert (resolved.user_id, resolved.organization_id, resolved.source) == (
        world.bob.id,
        world.org_a.id,
        "auto_ai",
    )
    assert resolved.id is not None


def test_the_resolver_never_commits(db_session, normal_user, commits):
    commits.clear()

    _resolve(db_session, normal_user, [word("one"), word("two")])

    assert commits == []
