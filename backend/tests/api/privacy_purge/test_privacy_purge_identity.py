"""A purge reverts the names and links a voiceprint made (#1280) and keeps what a person typed.

Fixtures (in-memory OpenSearch, recorded broker, in-process task runner) live in
``tests/api/privacy_purge/conftest.py``; seeding helpers in ``tests/fixtures/privacy_purge.py``.
"""

from __future__ import annotations

from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.models.speaker import SpeakerMatch
from app.models.speaker import SpeakerProfile
from tests.fixtures.privacy_purge import auto_accept_through_the_matcher
from tests.fixtures.privacy_purge import make_profile_row
from tests.fixtures.privacy_purge import make_speaker_row
from tests.fixtures.privacy_purge import run_org_purge
from tests.fixtures.privacy_purge import turn_off


def test_a_purge_removes_an_auto_accepted_name_and_link(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    profile = make_profile_row(gov, gov.alice, gov.org, "Alice")
    speaker = make_speaker_row(gov, gov.alice, gov.org, "SPEAKER_00")
    auto_accept_through_the_matcher(gov, gov.alice, speaker, profile)
    row = gov.db.get(Speaker, speaker.id)
    assert (row.display_name, row.profile_id, row.verified) == ("Alice", profile.id, True)
    assert row.identity_source == "voiceprint", "control: the matcher recorded its authorship"
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    row = gov.db.get(Speaker, speaker.id)
    assert (row.display_name, row.profile_id, row.verified, row.identity_source) == (
        None,
        None,
        False,
        None,
    )
    assert row.resolved_display_name == "SPEAKER_00", "the label is the diarizer's again"
    assert result["counts"]["postgres"]["speaker_identities_reverted"] == 1
    assert [f[1] for f in reindexed] == [str(speaker.media_file.uuid)], "its file is reindexed"
    assert gov.db.get(SpeakerProfile, profile.id).name == "Alice", "the profile itself stays"


def test_a_purge_keeps_a_typed_name_and_removes_only_a_machine_link(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    profile = make_profile_row(gov, gov.alice, gov.org, "Zed")
    typed = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Zed",
        verified=True,
        profile_id=profile.id,
        identity_source="voiceprint_link",
    )
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    row = gov.db.get(Speaker, typed.id)
    assert (row.display_name, row.verified, row.profile_id) == ("Zed", True, None)
    assert row.identity_source is None
    assert result["counts"]["postgres"]["speaker_links_removed"] == 1
    assert result["counts"]["postgres"]["speaker_identities_reverted"] == 0


def test_a_purge_never_touches_a_human_or_unknown_identity(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    profile = make_profile_row(gov, gov.alice, gov.org, "Bea")
    kept = {
        source: make_speaker_row(
            gov,
            gov.alice,
            gov.org,
            "SPEAKER_00",
            display_name="Bea",
            verified=True,
            profile_id=profile.id,
            identity_source=source,
        )
        for source in ("human", None)
    }
    machine = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Bea",
        verified=True,
        profile_id=profile.id,
        identity_source="voiceprint",
    )
    turn_off(gov.db, gov.org, gender=False)

    run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    for source, row in kept.items():
        fresh = gov.db.get(Speaker, row.id)
        assert (fresh.display_name, fresh.verified, fresh.profile_id) == ("Bea", True, profile.id)
        assert fresh.identity_source == source
    assert gov.db.get(Speaker, machine.id).display_name is None, "control: the machine one went"


def test_a_purge_in_one_organization_leaves_another_organizations_machine_identities(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    mine = make_speaker_row(
        gov, gov.alice, gov.org, "SPEAKER_00", display_name="Ann", identity_source="voiceprint"
    )
    theirs = make_speaker_row(
        gov,
        gov.outsider,
        gov.other_org,
        "SPEAKER_00",
        display_name="Ann",
        identity_source="voiceprint",
    )
    personal = make_speaker_row(
        gov, gov.alice, None, "SPEAKER_00", display_name="Ann", identity_source="voiceprint"
    )
    turn_off(gov.db, gov.org, gender=False)

    run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    assert gov.db.get(Speaker, mine.id).display_name is None
    for other in (theirs, personal):
        row = gov.db.get(Speaker, other.id)
        assert (row.display_name, row.identity_source) == ("Ann", "voiceprint")


def test_a_purge_takes_machine_members_out_of_a_promoted_cluster(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    profile = make_profile_row(gov, gov.alice, gov.org, "Ann")
    machine = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        profile_id=profile.id,
        identity_source="voiceprint",
    )
    human = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        profile_id=profile.id,
        identity_source="human",
    )
    solo_machine = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        profile_id=profile.id,
        identity_source="voiceprint",
    )

    def promoted(label, *members):
        cluster = SpeakerCluster(
            user_id=gov.alice.id,
            organization_id=gov.org.id,
            label=label,
            member_count=len(members),
            promoted_to_profile_id=profile.id,
        )
        gov.db.add(cluster)
        gov.db.flush()
        for member in members:
            gov.db.add(SpeakerClusterMember(cluster_id=cluster.id, speaker_id=member.id))
            member.cluster_id = cluster.id
        gov.db.commit()
        return cluster

    mixed_id = promoted("mixed", machine, human).id
    only_machine_id = promoted("only-machine", solo_machine).id
    machine_id, human_id = machine.id, human.id
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    members = {
        m.speaker_id
        for m in gov.db.query(SpeakerClusterMember).filter(
            SpeakerClusterMember.cluster_id == mixed_id
        )
    }
    assert members == {human_id}, "the machine member left; the human one stayed"
    assert gov.db.get(SpeakerCluster, mixed_id).member_count == 1
    assert gov.db.get(Speaker, machine_id).cluster_id is None
    assert gov.db.get(Speaker, human_id).cluster_id == mixed_id
    assert gov.db.get(SpeakerCluster, only_machine_id) is None, "an emptied cluster is deleted"
    postgres = result["counts"]["postgres"]
    assert postgres["promoted_cluster_members_removed"] == 2
    assert postgres["promoted_clusters_deleted"] == 1


def test_a_cross_scope_match_is_deleted_and_counted_and_the_other_speaker_is_untouched(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    # Created first so its id is LOWER than the in-scope speakers' (it becomes ``speaker1``),
    # and last so it is HIGHER: both sides of the pair table are exercised.
    outside_low = make_speaker_row(
        gov,
        gov.outsider,
        gov.other_org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        identity_source="voiceprint",
    )
    inside_a = make_speaker_row(gov, gov.alice, gov.org, "SPEAKER_00")
    inside_b = make_speaker_row(gov, gov.alice, gov.org, "SPEAKER_00")
    outside_high = make_speaker_row(gov, gov.outsider, gov.other_org, "SPEAKER_00")

    def match(first, second):
        low, high = sorted((first.id, second.id))
        gov.db.add(SpeakerMatch(speaker1_id=low, speaker2_id=high, confidence=0.9))

    match(inside_a, inside_b)  # both sides in scope
    match(outside_low, inside_a)  # the in-scope speaker is speaker2
    match(inside_b, outside_high)  # the in-scope speaker is speaker1
    match(outside_low, outside_high)  # entirely another tenant's
    gov.db.commit()
    kept = (outside_low.id, outside_high.id)
    survivor_id = outside_low.id
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    postgres = result["counts"]["postgres"]
    assert postgres["speaker_matches_deleted"] == 1
    assert postgres["cross_scope_matches"] == 2
    remaining = [
        (m.speaker1_id, m.speaker2_id)
        for m in gov.db.query(SpeakerMatch).filter(SpeakerMatch.speaker1_id.in_(kept))
    ]
    assert remaining == [tuple(sorted(kept))]
    survivor = gov.db.get(Speaker, survivor_id)
    assert (survivor.display_name, survivor.identity_source, survivor.verified) == (
        "Ann",
        "voiceprint",
        True,
    ), "the other scope's speaker row is never modified"


def test_a_voiceprint_suggestion_goes_with_the_identity_it_made_and_an_llm_one_stays(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    voice = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        identity_source="voiceprint",
        suggested_name="Ann",
        suggestion_source="profile_match",
        confidence=0.9,
    )
    llm = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        identity_source="voiceprint",
        suggested_name="Anne",
        suggestion_source="llm_analysis",
        confidence=0.8,
    )
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    cleared = gov.db.get(Speaker, voice.id)
    assert (cleared.suggested_name, cleared.suggestion_source, cleared.confidence) == (
        None,
        None,
        None,
    )
    kept = gov.db.get(Speaker, llm.id)
    assert (kept.suggested_name, kept.suggestion_source, kept.confidence) == (
        "Anne",
        "llm_analysis",
        0.8,
    )
    assert result["counts"]["postgres"]["suggestions_cleared"] == 1
