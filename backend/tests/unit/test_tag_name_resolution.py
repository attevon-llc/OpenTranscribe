"""Tag name normalization, cleaning and resolution onto one row per tenant.

Covers the pure name functions (exact transformations, shared byte-for-byte with the SPA's
``tagName.ts``) and the resolvers: normalized-exact matching only, one tenant plus the
system vocabulary, the legacy exact-name fallback, creation inside a savepoint, and the
fuzzy suggestion that never resolves anything by itself.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fixtures.tag_recorders import count_commits
from fixtures.tag_world import attach
from fixtures.tag_world import build_org_world
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import make_user
from fixtures.tag_world import word
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import OpenTranscribeError
from app.models.tag import Tag
from app.services import tag_service
from app.services.tag_service import MAX_TAG_NAME_LENGTH
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import clean_tag_name
from app.services.tag_service import lookup_existing_tag
from app.services.tag_service import names_are_similar
from app.services.tag_service import normalize_tag_name
from app.services.tag_service import resolve_or_create_tag
from app.services.tag_service import resolve_or_create_tags
from app.services.tag_service import stored_normalized_name
from app.services.tag_service import suggest_similar_tag

# ---------------------------------------------------------------------------
# Pure name functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Foo__Bar--baz  ", "foo bar baz"),
        ("-foo-", "foo"),
        ("a - b", "a b"),
        ("Q3_Earnings-Call", "q3 earnings call"),
        ("tab\tand\nnewline", "tab and newline"),
        ("non\u00a0breaking", "non breaking"),
        ("ÉCOLE", "école"),
        ("C++ / Go 🚀", "c++ / go 🚀"),
        ("\u0130", "i\u0307"),
        ("-", ""),
        ("__", ""),
        (" - _ ", ""),
        ("", ""),
    ],
)
def test_normalize_tag_name_transforms_exactly(raw, expected):
    assert normalize_tag_name(raw) == expected


def test_normalized_form_is_not_length_clamped():
    assert normalize_tag_name("x" * 80) == "x" * 80


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Mixed Case  Name ", "Mixed Case  Name"),
        ("", ""),
        ("a" * 60, "a" * 50),
        ("é" * 60, "é" * 50),
        ("a" * 49 + " tail", "a" * 49),
    ],
)
def test_clean_tag_name_strips_and_clamps_by_characters(raw, expected):
    assert clean_tag_name(raw) == expected


def test_storage_width_and_error_type():
    assert MAX_TAG_NAME_LENGTH == 50
    assert issubclass(InvalidTagNameError, OpenTranscribeError)


def test_stored_normalized_name_prefers_the_stored_value():
    assert stored_normalized_name(SimpleNamespace(name="Other", normalized_name="kept")) == "kept"
    assert stored_normalized_name(SimpleNamespace(name="Re-Do", normalized_name=None)) == "re do"
    assert stored_normalized_name(SimpleNamespace(name="Re_Do", normalized_name="")) == "re do"


@pytest.mark.parametrize(
    ("a", "b", "threshold", "expected"),
    [
        ("Board-Meeting", "board meeting", 0.99, True),
        ("q3-earnings", "q4-earnings", 0.85, True),
        ("q3-earnings", "q4-earnings", 0.95, False),
        ("alpha", "zulu", 0.85, False),
        ("-", "-", 0.0, False),
        ("", "anything", 0.0, False),
    ],
)
def test_names_are_similar(a, b, threshold, expected):
    assert names_are_similar(a, b, threshold) is expected


# ---------------------------------------------------------------------------
# Single resolver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "-", "__", " - _ "])
def test_a_blank_name_raises_and_stages_nothing(db_session, normal_user, blank):
    with pytest.raises(InvalidTagNameError):
        resolve_or_create_tag(db_session, blank, user_id=normal_user.id, organization_id=None)
    assert [obj for obj in db_session.new if isinstance(obj, Tag)] == []


def test_variants_resolve_to_the_first_row(db_session, normal_user):
    stem = word("Quarterly")
    first = resolve_or_create_tag(
        db_session, f"{stem} Review", user_id=normal_user.id, organization_id=None
    )
    variants = [
        f"{stem.upper()}_REVIEW",
        f"{stem.lower()}-review",
        f"  {stem}   review ",
        f"{stem}--__Review",
    ]
    resolved = [
        resolve_or_create_tag(db_session, v, user_id=normal_user.id, organization_id=None).id
        for v in variants
    ]
    assert resolved == [first.id] * len(variants)
    assert first.name == f"{stem} Review"
    assert first.normalized_name == normalize_tag_name(f"{stem} Review")


def test_a_fuzzy_near_match_creates_a_new_row(db_session, normal_user):
    stem = word("fz")
    q3 = resolve_or_create_tag(
        db_session, f"{stem}-q3-earnings", user_id=normal_user.id, organization_id=None
    )
    q4 = resolve_or_create_tag(
        db_session, f"{stem}-q4-earnings", user_id=normal_user.id, organization_id=None
    )
    assert q4.id != q3.id
    assert q4.name == f"{stem}-q4-earnings"


def test_a_new_row_is_flushed_attributed_and_carries_the_source(db_session, normal_user):
    tag = resolve_or_create_tag(
        db_session,
        f"  {word('Src')}  ",
        user_id=normal_user.id,
        organization_id=None,
        source="auto_ai",
    )
    assert tag.id is not None
    assert tag.user_id == normal_user.id
    assert tag.organization_id is None
    assert tag.source == "auto_ai"
    assert tag.name == tag.name.strip()


def test_an_over_long_name_is_stored_clamped_and_reached_from_both_paths(db_session, normal_user):
    long_name = word("Long") + "-" + "x" * 70
    single = resolve_or_create_tag(
        db_session, long_name, user_id=normal_user.id, organization_id=None
    )
    assert single.name == long_name[:50]
    batch = resolve_or_create_tags(
        db_session, ["", long_name], user_id=normal_user.id, organization_id=None
    )
    assert [tag.id for tag in batch] == [single.id]


def test_resolution_never_commits(db_session, normal_user, monkeypatch):
    commits = count_commits(monkeypatch, db_session)
    resolve_or_create_tag(db_session, word("nc"), user_id=normal_user.id, organization_id=None)
    resolve_or_create_tags(
        db_session, [word("nc"), word("nc")], user_id=normal_user.id, organization_id=None
    )
    assert commits == []


def test_a_legacy_row_without_normalization_is_found_by_exact_name_and_repaired(
    db_session, normal_user
):
    name = word("Legacy Name")
    legacy = Tag(name=name, user_id=normal_user.id, normalized_name=None, source=None)
    db_session.add(legacy)
    db_session.commit()

    found = resolve_or_create_tag(db_session, name, user_id=normal_user.id, organization_id=None)
    assert found.id == legacy.id
    assert found.normalized_name == normalize_tag_name(name)

    variant = resolve_or_create_tag(
        db_session, name.upper(), user_id=normal_user.id, organization_id=None
    )
    assert variant.id == legacy.id
    assert (
        db_session.query(Tag).filter(Tag.normalized_name == normalize_tag_name(name)).count() == 1
    )


def test_another_users_same_named_tag_is_never_resolved_onto(db_session, normal_user, other_user):
    name = word("Theirs")
    theirs = make_tag(db_session, name, user_id=other_user.id)
    mine = resolve_or_create_tag(db_session, name, user_id=normal_user.id, organization_id=None)
    assert mine.id != theirs.id
    assert mine.user_id == normal_user.id


def test_an_own_row_beats_a_same_named_system_row(db_session, normal_user):
    name = word("Shared")
    make_tag(db_session, name, user_id=None)
    own = make_tag(db_session, name, user_id=normal_user.id)
    resolved = resolve_or_create_tag(
        db_session, name.lower(), user_id=normal_user.id, organization_id=None
    )
    assert resolved.id == own.id


def test_a_system_row_is_reused_not_forked(db_session, normal_user):
    system = make_tag(db_session, word("Seeded"), user_id=None)
    resolved = resolve_or_create_tag(
        db_session, system.name.upper(), user_id=normal_user.id, organization_id=None
    )
    assert resolved.id == system.id
    assert db_session.query(Tag).filter(Tag.normalized_name == system.normalized_name).count() == 1


def test_organization_members_resolve_onto_one_org_row(db_session):
    w = build_org_world(db_session)
    name = word("Roadmap")
    created = resolve_or_create_tag(
        db_session, name, user_id=w.alice.id, organization_id=w.org_a.id
    )
    reused = resolve_or_create_tag(
        db_session, name.upper(), user_id=w.bob.id, organization_id=w.org_a.id
    )
    personal = resolve_or_create_tag(db_session, name, user_id=w.bob.id, organization_id=None)
    other_org = resolve_or_create_tag(
        db_session, name, user_id=w.alice.id, organization_id=w.org_b.id
    )

    assert reused.id == created.id
    assert (created.organization_id, created.user_id) == (w.org_a.id, w.alice.id)
    assert personal.id != created.id and personal.organization_id is None
    assert other_org.id not in {created.id, personal.id}
    assert other_org.organization_id == w.org_b.id


def test_the_file_aware_resolver_reuses_the_row_already_on_a_shared_file(
    db_session, normal_user, other_user
):
    name = word("OnFile")
    owners_tag = make_tag(db_session, name, user_id=normal_user.id)
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, owners_tag)

    with_file = resolve_or_create_tag(
        db_session, name, user_id=other_user.id, organization_id=None, file_id=media_file.id
    )
    without_file = resolve_or_create_tag(
        db_session, name, user_id=other_user.id, organization_id=None
    )
    assert with_file.id == owners_tag.id
    assert without_file.id != owners_tag.id
    assert without_file.user_id == other_user.id


def test_a_lost_creation_race_returns_the_winner_and_keeps_earlier_work(
    db_session, normal_user, monkeypatch
):
    name = word("Raced")
    winner = make_tag(db_session, name, user_id=normal_user.id)
    earlier = Tag(name=word("earlier"), user_id=normal_user.id, normalized_name="earlier pending")
    db_session.add(earlier)
    db_session.flush()

    real_lookup = tag_service.lookup_existing_tag
    misses = iter([True])

    def first_lookup_misses(*args, **kwargs):
        if next(misses, False):
            return None
        return real_lookup(*args, **kwargs)

    monkeypatch.setattr(tag_service, "lookup_existing_tag", first_lookup_misses)
    resolved = resolve_or_create_tag(db_session, name, user_id=normal_user.id, organization_id=None)

    assert resolved.id == winner.id
    assert db_session.query(Tag).filter(Tag.id == earlier.id).one().name == earlier.name


def test_a_lost_race_with_no_visible_winner_re_raises(db_session, normal_user, monkeypatch):
    name = word("Ghost")
    make_tag(db_session, name, user_id=normal_user.id)
    monkeypatch.setattr(tag_service, "lookup_existing_tag", lambda *a, **k: None)
    with pytest.raises(IntegrityError):
        resolve_or_create_tag(db_session, name, user_id=normal_user.id, organization_id=None)


def test_lookup_existing_tag_returns_none_when_nothing_matches(db_session, normal_user):
    name = word("Absent")
    assert (
        lookup_existing_tag(db_session, normalize_tag_name(name), name, normal_user.id, None)
        is None
    )


# ---------------------------------------------------------------------------
# Fuzzy suggestion
# ---------------------------------------------------------------------------


def test_a_suggestion_comes_from_the_callers_own_vocabulary(db_session, normal_user):
    stem = word("sg")
    mine = make_tag(db_session, f"{stem}-q3-earnings", user_id=normal_user.id)
    suggested = suggest_similar_tag(
        db_session, f"{stem}-q4-earnings", user_id=normal_user.id, organization_id=None
    )
    assert suggested is not None and suggested.id == mine.id


def test_a_suggestion_never_crosses_into_another_account(db_session, normal_user):
    stem = word("sx")
    stranger = make_user(db_session, "stranger")
    make_tag(db_session, f"{stem}-q3-earnings", user_id=stranger.id)
    assert (
        suggest_similar_tag(
            db_session, f"{stem}-q4-earnings", user_id=normal_user.id, organization_id=None
        )
        is None
    )


def test_a_supplied_candidate_pool_replaces_the_query(db_session, normal_user):
    stem = word("pool")
    make_tag(db_session, f"{stem}-q3-earnings", user_id=normal_user.id)
    pooled = Tag(name=f"{stem}-q3-earnlngs", user_id=normal_user.id)
    assert (
        suggest_similar_tag(
            db_session,
            f"{stem}-q3-earnings",
            user_id=normal_user.id,
            organization_id=None,
            candidates=[pooled],
        )
        is pooled
    )
    assert (
        suggest_similar_tag(
            db_session,
            f"{stem}-q3-earnings",
            user_id=normal_user.id,
            organization_id=None,
            candidates=[],
        )
        is None
    )


def test_a_blank_name_gets_no_suggestion(db_session, normal_user):
    make_tag(db_session, word("any"), user_id=normal_user.id)
    assert (
        suggest_similar_tag(
            db_session, " - ", user_id=normal_user.id, organization_id=None, threshold=0.0
        )
        is None
    )
