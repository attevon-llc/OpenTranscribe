"""Behavioral tests for collision clustering and the tag-list narrowing filters.

Covers ``app/services/tag_collisions.py``. The scenarios that matter here are the
ones a naive implementation gets wrong:

* **Clusters are grouped on the stored normalization, never fuzzily.** The fuzzy
  matcher is ``difflib.SequenceMatcher`` at 0.85, which is non-transitive, so
  clusters built on it would reshuffle between page loads. Fuzzy hits come back
  as a ranked *secondary* suggestion per cluster and are never cluster members.
* **Stored normalization drifts.** Migration v230 backfilled ``normalized_name``
  but nothing keeps it true, and the bootstrap seed tags were inserted NULL. A
  collision pass that trusted the column would miss exactly the legacy rows it
  exists to find, so the pass repairs the column first.
* **Unused and ``usage_count`` must agree.** Both are scoped to the caller's
  accessible files; a tag whose only files belong to someone else reads as
  ``usage_count: 0`` *and* as unused, never one without the other.
"""

from __future__ import annotations

import uuid

from app.core.constants import TAG_SOURCE_AI_ACCEPTED
from app.core.constants import TAG_SOURCE_AUTO_AI
from app.core.constants import TAG_SOURCE_MANUAL
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.services.tag_collisions import accessible_usage_counts
from app.services.tag_collisions import find_tag_collisions
from app.services.tag_collisions import list_tags_filtered
from app.services.tag_collisions import list_unused_tag_rows
from app.services.tag_collisions import refresh_stored_normalization
from app.services.tag_service import normalize_tag_name


def _suffix() -> str:
    """Tag names are globally unique — every created name needs its own suffix."""
    return uuid.uuid4().hex[:8]


def _raw_tag(
    db_session,
    name: str,
    *,
    normalized: str | None = ...,
    source=TAG_SOURCE_MANUAL,
    user_id: int | None = None,
):
    """Insert a tag row directly, so a *broken* stored normalization can be staged.

    The resolver would never produce two rows sharing a normalized name — these
    collisions predate it, so the test has to write them the way history did.

    ``user_id=None`` writes a **system** tag, which is in every caller's
    ``owned_or_system`` scope; pass an owner when the test is about one
    account's vocabulary specifically.
    """
    stored = normalize_tag_name(name) if normalized is ... else normalized
    tag = Tag(name=name, user_id=user_id, source=source, normalized_name=stored)
    db_session.add(tag)
    db_session.flush()
    return tag


def _make_file(db_session, owner) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="tag_collision_test.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _attach(db_session, media_file, tag, *, source=TAG_SOURCE_MANUAL) -> FileTag:
    link = FileTag(media_file_id=media_file.id, tag_id=tag.id, source=source)
    db_session.add(link)
    db_session.flush()
    return link


def _cluster_for(clusters, normalized: str):
    """Pick this test's cluster out of the install-wide pass."""
    matches = [cluster for cluster in clusters if cluster.normalized_name == normalized]
    assert len(matches) == 1, f"expected exactly one cluster for {normalized!r}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def test_case_variants_land_in_one_cluster(db_session, normal_user):
    """Two names differing only by case are one collision, not two tags."""
    suffix = _suffix()
    lower = _raw_tag(db_session, f"interview-{suffix}")
    upper = _raw_tag(db_session, f"INTERVIEW-{suffix}")

    clusters = find_tag_collisions(db_session, user_id=normal_user.id)

    cluster = _cluster_for(clusters, normalize_tag_name(lower.name))
    assert {member.uuid for member in cluster.members} == {lower.uuid, upper.uuid}


def test_cluster_of_three_preselects_the_highest_usage_survivor(db_session, normal_user):
    """Every member comes back, with the most-used one marked as the survivor."""
    suffix = _suffix()
    quiet = _raw_tag(db_session, f"budget-{suffix}")
    busiest = _raw_tag(db_session, f"Budget_{suffix}")
    middle = _raw_tag(db_session, f"BUDGET {suffix}")

    for _ in range(3):
        _attach(db_session, _make_file(db_session, normal_user), busiest)
    _attach(db_session, _make_file(db_session, normal_user), middle)

    clusters = find_tag_collisions(db_session, user_id=normal_user.id)
    cluster = _cluster_for(clusters, normalize_tag_name(quiet.name))

    assert {member.uuid for member in cluster.members} == {quiet.uuid, busiest.uuid, middle.uuid}
    assert cluster.suggested_survivor_uuid == busiest.uuid
    assert [member.uuid for member in cluster.members if member.suggested_survivor] == [
        busiest.uuid
    ]
    # Highest usage first, so the survivor is also the head of the list.
    assert [member.usage_count for member in cluster.members] == [3, 1, 0]


def test_repeated_passes_return_the_same_order(db_session, normal_user):
    """Clusters and their suggestions are stable between requests."""
    suffix = _suffix()
    _raw_tag(db_session, f"q3-earnings-{suffix}")
    _raw_tag(db_session, f"Q3 Earnings {suffix}")
    _raw_tag(db_session, f"q4-earnings-{suffix}")
    _raw_tag(db_session, f"Q4 Earnings {suffix}")

    first = find_tag_collisions(db_session, user_id=normal_user.id)
    second = find_tag_collisions(db_session, user_id=normal_user.id)

    def shape(clusters):
        return [
            (
                cluster.normalized_name,
                [member.uuid for member in cluster.members],
                cluster.suggested_survivor_uuid,
                [(s.uuid, s.similarity) for s in cluster.suggestions],
            )
            for cluster in clusters
        ]

    assert shape(first) == shape(second)


def test_fuzzy_near_matches_are_suggestions_not_members(db_session, normal_user):
    """A near match is offered beside the cluster, never folded into it."""
    suffix = _suffix()
    exact_a = _raw_tag(db_session, f"q3-earnings-{suffix}")
    exact_b = _raw_tag(db_session, f"Q3 Earnings {suffix}")
    near = _raw_tag(db_session, f"q4-earnings-{suffix}")

    clusters = find_tag_collisions(db_session, user_id=normal_user.id)
    cluster = _cluster_for(clusters, normalize_tag_name(exact_a.name))

    assert {member.uuid for member in cluster.members} == {exact_a.uuid, exact_b.uuid}
    assert near.uuid not in {member.uuid for member in cluster.members}
    assert near.uuid in {suggestion.uuid for suggestion in cluster.suggestions}
    similarities = [suggestion.similarity for suggestion in cluster.suggestions]
    assert similarities == sorted(similarities, reverse=True)


# ---------------------------------------------------------------------------
# Stored-normalization repair
# ---------------------------------------------------------------------------


def test_stale_stored_normalization_is_corrected_and_clusters(db_session, normal_user):
    """A row whose stored normalization disagrees with the name is repaired."""
    suffix = _suffix()
    stale = _raw_tag(db_session, f"retro-{suffix}", normalized="something-else-entirely")
    partner = _raw_tag(db_session, f"RETRO {suffix}")
    expected = normalize_tag_name(stale.name)

    clusters = find_tag_collisions(db_session, user_id=normal_user.id)

    db_session.refresh(stale)
    assert stale.normalized_name == expected
    cluster = _cluster_for(clusters, expected)
    assert {member.uuid for member in cluster.members} == {stale.uuid, partner.uuid}


def test_null_stored_normalization_is_corrected_and_clusters(db_session, normal_user):
    """A legacy NULL normalization is filled in and then collides like any other."""
    suffix = _suffix()
    legacy = _raw_tag(db_session, f"standup-{suffix}", normalized=None)
    partner = _raw_tag(db_session, f"STANDUP {suffix}")
    expected = normalize_tag_name(legacy.name)

    clusters = find_tag_collisions(db_session, user_id=normal_user.id)

    db_session.refresh(legacy)
    assert legacy.normalized_name == expected
    cluster = _cluster_for(clusters, expected)
    assert {member.uuid for member in cluster.members} == {legacy.uuid, partner.uuid}


def test_refresh_stored_normalization_is_idempotent(db_session, normal_user):
    """Running the repair twice changes nothing the second time."""
    suffix = _suffix()
    _raw_tag(db_session, f"idem-{suffix}", normalized=None, user_id=normal_user.id)
    _raw_tag(db_session, f"idem-two-{suffix}", normalized="wrong", user_id=normal_user.id)

    first = refresh_stored_normalization(db_session, user_id=normal_user.id)
    second = refresh_stored_normalization(db_session, user_id=normal_user.id)

    assert first >= 2
    assert second == 0


# ---------------------------------------------------------------------------
# Listing filters
# ---------------------------------------------------------------------------


def test_unused_filter_and_usage_count_agree_for_inaccessible_files(
    db_session, normal_user, other_user
):
    """A tag whose only files belong to someone else reads as unused *and* as 0.

    ``list_unused_tags`` used to count usage globally while ``list_tags`` counted
    it scoped, so this tag showed ``usage_count: 0`` in the list and was absent
    from the unused set. Both now read through the same accessible-file gate.
    """
    tag = _raw_tag(db_session, f"private-{_suffix()}")
    _attach(db_session, _make_file(db_session, other_user), tag)

    counts = accessible_usage_counts(db_session, user_id=normal_user.id)
    listed = {entry.uuid: entry for entry in list_tags_filtered(db_session, user_id=normal_user.id)}
    unused = list_tags_filtered(db_session, user_id=normal_user.id, unused=True)
    unused_rows = list_unused_tag_rows(db_session, user_id=normal_user.id)

    assert counts.get(tag.id, 0) == 0
    assert listed[tag.uuid].usage_count == 0
    assert tag.uuid in {entry.uuid for entry in unused}
    assert tag.uuid in {row.uuid for row in unused_rows}


def test_unused_filter_excludes_a_tag_the_caller_can_see_in_use(db_session, normal_user):
    """The mirror case: a tag on the caller's own file is used, so not unused."""
    tag = _raw_tag(db_session, f"mine-{_suffix()}")
    _attach(db_session, _make_file(db_session, normal_user), tag)

    listed = {entry.uuid: entry for entry in list_tags_filtered(db_session, user_id=normal_user.id)}
    unused = list_tags_filtered(db_session, user_id=normal_user.id, unused=True)

    assert listed[tag.uuid].usage_count == 1
    assert tag.uuid not in {entry.uuid for entry in unused}


def test_awaiting_review_filter_covers_only_the_auto_labelers_tags(db_session, normal_user):
    """Manual, accepted, and legacy NULL origins are not awaiting review."""
    suffix = _suffix()
    auto = _raw_tag(db_session, f"auto-{suffix}", source=TAG_SOURCE_AUTO_AI)
    manual = _raw_tag(db_session, f"manual-{suffix}", source=TAG_SOURCE_MANUAL)
    accepted = _raw_tag(db_session, f"accepted-{suffix}", source=TAG_SOURCE_AI_ACCEPTED)
    legacy = _raw_tag(db_session, f"legacy-{suffix}", source=None)

    awaiting = {
        entry.uuid
        for entry in list_tags_filtered(db_session, user_id=normal_user.id, awaiting_review=True)
    }

    assert auto.uuid in awaiting
    assert manual.uuid not in awaiting
    assert accepted.uuid not in awaiting
    assert legacy.uuid not in awaiting


def test_list_entries_carry_the_awaiting_review_flag(db_session, normal_user):
    """The SPA renders the badge; it does not re-derive it from the origin string."""
    suffix = _suffix()
    auto = _raw_tag(db_session, f"flagged-{suffix}", source=TAG_SOURCE_AUTO_AI)
    manual = _raw_tag(db_session, f"unflagged-{suffix}", source=TAG_SOURCE_MANUAL)

    listed = {entry.uuid: entry for entry in list_tags_filtered(db_session, user_id=normal_user.id)}

    assert listed[auto.uuid].awaiting_review is True
    assert listed[manual.uuid].awaiting_review is False


def test_colliding_filter_narrows_to_cluster_members(db_session, normal_user):
    """The colliding filter returns exactly the tags a cluster claims."""
    suffix = _suffix()
    first = _raw_tag(db_session, f"dupe-{suffix}")
    second = _raw_tag(db_session, f"DUPE-{suffix}")
    alone = _raw_tag(db_session, f"unique-{suffix}")

    colliding = {
        entry.uuid
        for entry in list_tags_filtered(db_session, user_id=normal_user.id, colliding=True)
    }

    assert {first.uuid, second.uuid} <= colliding
    assert alone.uuid not in colliding


def test_filters_combine(db_session, normal_user):
    """Filters narrow together rather than replacing one another."""
    suffix = _suffix()
    auto_used = _raw_tag(db_session, f"combo-used-{suffix}", source=TAG_SOURCE_AUTO_AI)
    auto_unused = _raw_tag(db_session, f"combo-free-{suffix}", source=TAG_SOURCE_AUTO_AI)
    _attach(db_session, _make_file(db_session, normal_user), auto_used)

    narrowed = {
        entry.uuid
        for entry in list_tags_filtered(
            db_session, user_id=normal_user.id, awaiting_review=True, unused=True
        )
    }

    assert auto_unused.uuid in narrowed
    assert auto_used.uuid not in narrowed
