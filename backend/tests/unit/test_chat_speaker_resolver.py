"""W2.2: deterministic, Postgres-only speaker-mention resolution.

Three groups:

1. Candidate extraction — the English-first capitalization heuristics, no DB.
2. The matching ladder (exact -> unique token-subset -> fuzzy) against a
   synthetic :class:`Roster`, no DB.
3. Roster access control against a REAL database — permission matrix row T3:
   LEAK (an unshared speaker is unresolvable and absent from the resolution)
   and SHARED (a mention resolves on a genuinely shared file). Asserted
   against real share rows, per the root CLAUDE.md's #385 lesson that a
   vacuous shared-visibility test is how that class of bug survives review.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

from app.services.chat.speaker_resolver import MAX_RESOLUTION_ITEMS
from app.services.chat.speaker_resolver import Roster
from app.services.chat.speaker_resolver import RosterEntry
from app.services.chat.speaker_resolver import build_roster
from app.services.chat.speaker_resolver import extract_candidates
from app.services.chat.speaker_resolver import has_speaker_verb_frame
from app.services.chat.speaker_resolver import match_candidate
from app.services.chat.speaker_resolver import resolve_speaker_mentions

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------


def test_multi_word_name_is_one_candidate_not_two():
    candidates = extract_candidates("What did Alice Chen say about pricing?")
    assert "Alice Chen" in candidates
    assert "Alice" not in candidates
    assert "Chen" not in candidates


def test_ordinary_capitalized_name_is_a_candidate_anywhere():
    assert "Dana" in extract_candidates("What did Dana say about pricing?")
    assert "Dana" in extract_candidates("Dana, what did you commit to?")


def test_lowercase_common_word_is_never_a_candidate():
    """The brief's own example: 'will' in 'who will own this' carries no
    capitalization at all, so it can never reach the roster-matching stage."""
    assert extract_candidates("who will own this") == []


def test_capitalized_common_word_mid_sentence_is_a_candidate():
    candidates = extract_candidates("Did Grace present the report?")
    assert "Grace" in candidates


def test_capitalized_common_word_at_sentence_start_is_not_a_candidate():
    """Sentence-initial capitalization is orthographic convention in English,
    not evidence of a proper noun — a known, documented false negative."""
    assert "Will" not in extract_candidates("Will this be ready by Friday?")


def test_capitalized_common_word_after_a_full_stop_is_sentence_initial():
    candidates = extract_candidates("We finished the review. Grace joined late.")
    # "Grace" opens its own sentence here — same rule as the very first word.
    assert "Grace" not in candidates


def test_non_common_capitalized_word_is_a_candidate_even_at_sentence_start():
    assert "Dana" in extract_candidates("Dana asked about the timeline.")


def test_empty_text_yields_no_candidates():
    assert extract_candidates("") == []
    assert extract_candidates("no capitals here at all") == []


# ---------------------------------------------------------------------------
# Matching ladder
# ---------------------------------------------------------------------------


def _roster(*names: str) -> Roster:
    return Roster(entries=tuple(RosterEntry(name=n, profile_id=None, file_count=1) for n in names))


def test_exact_match_case_and_width_insensitive():
    roster = _roster("Alice Chen")
    outcome = match_candidate("alice chen", roster)
    assert outcome.matched == "Alice Chen"
    assert outcome.ambiguous_with == ()


def test_unique_token_subset_match():
    roster = _roster("Alice Chen")
    outcome = match_candidate("Alice", roster)
    assert outcome.matched == "Alice Chen"


def test_ambiguous_token_subset_resolves_to_no_match():
    """Two roster entries both contain 'Alice' as a token — ambiguity, not a
    guess. The design constraint: ambiguity means no filter, ever."""
    roster = _roster("Alice Chen", "Alice Ng")
    outcome = match_candidate("Alice", roster)
    assert outcome.matched is None
    assert set(outcome.ambiguous_with) == {"Alice Chen", "Alice Ng"}


def test_exact_match_short_circuits_before_the_ambiguous_subset_rung():
    """An exact hit is unique even when OTHER roster entries would make a
    token-subset match ambiguous — exact must win outright."""
    roster = _roster("Alice", "Alice Chen")
    outcome = match_candidate("Alice", roster)
    assert outcome.matched == "Alice"


def test_fuzzy_match_tolerates_a_short_typo():
    roster = _roster("Alice")
    outcome = match_candidate("Alicce", roster)  # ratio 0.909, above the 0.85 floor
    assert outcome.matched == "Alice"


def test_fuzzy_match_below_threshold_is_a_miss():
    roster = _roster("Alice")
    outcome = match_candidate("Zephyr", roster)
    assert outcome.matched is None
    assert outcome.ambiguous_with == ()
    assert outcome.reason == "no_roster_match"


def test_no_roster_match_reports_a_reason():
    roster = _roster("Bob")
    outcome = match_candidate("Nobody", roster)
    assert outcome.reason == "no_roster_match"


# ---------------------------------------------------------------------------
# Speaker-verb frame
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "What did Dana say about pricing?",
        "Did Dana mention the budget?",
        "What was Dana's opinion on the redesign?",
        "What did Dana think of the proposal?",
        # #523: the exact probe wording that found this lexicon gap — a
        # "contribute" frame was previously absent, so a clearly
        # speaker-scoped question never reached the parallel speaker leg.
        "What did the Marketing role contribute across the TS3005 meeting series?",
        "What did Dana contribute to the plan?",
    ],
)
def test_speaker_verb_frame_detected(text):
    assert has_speaker_verb_frame(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "The meeting with Dana ran long.",
        "Dana's slides are in the shared folder.",
        "Reschedule the call with Dana for Tuesday.",
    ],
)
def test_no_speaker_verb_frame(text):
    assert has_speaker_verb_frame(text) is False


# ---------------------------------------------------------------------------
# resolve_speaker_mentions: soft design constraint + size caps (no DB — a
# roster is supplied directly)
# ---------------------------------------------------------------------------


def test_unique_match_plus_verb_frame_sets_speaker_focus(db_session):
    roster = _roster("Dana")
    result = resolve_speaker_mentions(
        db_session, "What did Dana say about pricing?", user_id=1, roster=roster
    )
    assert result.matched == ("Dana",)
    assert result.speaker_focus is True


def test_role_labeled_speaker_contribution_question_sets_speaker_focus(db_session):
    """#523's reproduction case, at the resolver level: a role used as the
    diarized speaker label ("Marketing") asked about with "contribute" now
    resolves to a unique match with speaker focus — the router-level
    integration (routing this into a PARALLEL retrieval leg) is asserted in
    ``test_chat_router_speaker_focus.py``/``test_chat_retrieval_speaker_focus.py``;
    this pins the resolver half that used to silently decline."""
    roster = _roster("Marketing", "Engineering")
    result = resolve_speaker_mentions(
        db_session,
        "What did the Marketing role contribute across the TS3005 meeting series?",
        user_id=1,
        roster=roster,
    )
    assert result.matched == ("Marketing",)
    assert result.speaker_focus is True


def test_match_without_verb_frame_does_not_set_speaker_focus(db_session):
    roster = _roster("Dana")
    result = resolve_speaker_mentions(
        db_session, "Reschedule the call with Dana for Tuesday.", user_id=1, roster=roster
    )
    assert result.matched == ("Dana",)
    assert result.speaker_focus is False


def test_ambiguity_never_sets_speaker_focus(db_session):
    """Ambiguity => no filter: even with a verb frame present, an ambiguous
    mention must never widen retrieval."""
    roster = _roster("Alice Chen", "Alice Ng")
    result = resolve_speaker_mentions(
        db_session, "What did Alice say about pricing?", user_id=1, roster=roster
    )
    assert result.matched == ()
    assert "Alice" in result.ambiguous
    assert result.speaker_focus is False


def test_no_roster_entries_resolves_nothing(db_session):
    result = resolve_speaker_mentions(
        db_session, "What did Dana say?", user_id=1, roster=Roster(entries=())
    )
    assert result.matched == ()
    assert result.declined is False


def test_declined_roster_resolves_nothing_and_flags_declined(db_session):
    result = resolve_speaker_mentions(
        db_session, "What did Dana say?", user_id=1, roster=Roster(entries=(), declined=True)
    )
    assert result.matched == ()
    assert result.declined is True
    assert result.as_meta() == {"declined": True}


def test_resolution_lists_are_size_capped(db_session):
    names = [f"Person{i} Surname{i}" for i in range(MAX_RESOLUTION_ITEMS + 10)]
    roster = _roster(*names)
    question = " ".join(f"What did {n} say?" for n in names)
    result = resolve_speaker_mentions(db_session, question, user_id=1, roster=roster)
    assert len(result.matched) <= MAX_RESOLUTION_ITEMS


def test_as_meta_omits_empty_fields():
    from app.services.chat.speaker_resolver import SpeakerMentionResolution

    assert SpeakerMentionResolution().as_meta() == {}


def test_as_meta_shape():
    from app.services.chat.speaker_resolver import SpeakerMentionResolution

    result = SpeakerMentionResolution(matched=("Dana",), ambiguous=("Alice",))
    assert result.as_meta() == {"matched": ["Dana"], "ambiguous": ["Alice"]}


# ---------------------------------------------------------------------------
# Roster: permission matrix row T3 — real DB, real share rows.
# ---------------------------------------------------------------------------


def _make_file(db, user, *, title="Recording", organization_id=None, quarantined=False):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        organization_id=organization_id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
        is_quarantined=quarantined,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def _share_with(db, owner, recipient, media_file, *, permission="viewer") -> None:
    from app.models.media import Collection
    from app.models.media import CollectionMember
    from app.models.sharing import CollectionShare

    collection = Collection(
        user_id=owner.id, name=f"share-{uuid_pkg.uuid4().hex[:8]}", description="w2.2 test"
    )
    db.add(collection)
    db.commit()
    db.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=recipient.id,
            permission=permission,
        )
    )
    db.commit()


def _add_speaker(db, media_file, owner, *, display_name, name="SPEAKER_00"):
    from app.models.media import Speaker

    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        media_file_id=media_file.id,
        name=name,
        display_name=display_name,
        verified=True,
    )
    db.add(speaker)
    db.commit()
    db.refresh(speaker)
    return speaker


def test_t3_leak_an_unshared_speaker_is_unresolvable_and_absent(
    db_session, normal_user, other_user
):
    unshared = _make_file(db_session, other_user, title="Not shared")
    _add_speaker(db_session, unshared, other_user, display_name="Priya Patel")

    roster = build_roster(db_session, normal_user.id)
    assert "Priya Patel" not in {e.name for e in roster.entries}

    result = resolve_speaker_mentions(
        db_session, "What did Priya Patel say about the roadmap?", user_id=normal_user.id
    )
    assert result.matched == ()
    assert result.as_meta().get("matched") is None


def test_t3_shared_resolves_on_a_genuinely_shared_file(db_session, normal_user, other_user):
    shared = _make_file(db_session, other_user, title="Shared with me")
    _add_speaker(db_session, shared, other_user, display_name="Priya Patel")
    _share_with(db_session, other_user, normal_user, shared)

    roster = build_roster(db_session, normal_user.id)
    assert "Priya Patel" in {e.name for e in roster.entries}

    result = resolve_speaker_mentions(
        db_session, "What did Priya Patel say about the roadmap?", user_id=normal_user.id
    )
    assert result.matched == ("Priya Patel",)


def test_roster_never_uses_speaker_user_id_as_the_access_gate(db_session, normal_user, other_user):
    """The single highest-risk detail in the brief: `Speaker.user_id` is the
    file OWNER's id, and scoping the roster to it (rather than to accessible
    files) would silently drop every speaker on a shared recording. This test
    fails if `build_roster` is ever rewritten to filter on `Speaker.user_id
    == user_id` instead of the accessible-files subquery."""
    shared = _make_file(db_session, other_user, title="Shared with me")
    speaker = _add_speaker(db_session, shared, other_user, display_name="Priya Patel")
    assert speaker.user_id == other_user.id  # owner-attributed, not the reader's id
    _share_with(db_session, other_user, normal_user, shared)

    roster = build_roster(db_session, normal_user.id)
    assert "Priya Patel" in {e.name for e in roster.entries}


def test_roster_excludes_quarantined_files(db_session, normal_user):
    quarantined = _make_file(db_session, normal_user, title="Quarantined", quarantined=True)
    _add_speaker(db_session, quarantined, normal_user, display_name="Priya Patel")

    roster = build_roster(db_session, normal_user.id)
    assert "Priya Patel" not in {e.name for e in roster.entries}


def test_roster_reports_profile_id_and_file_count(db_session, normal_user):
    from app.models.media import SpeakerProfile

    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="Priya")
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)

    file_one = _make_file(db_session, normal_user, title="One")
    file_two = _make_file(db_session, normal_user, title="Two")
    for f in (file_one, file_two):
        speaker = _add_speaker(db_session, f, normal_user, display_name="Priya Patel")
        speaker.profile_id = profile.id
        db_session.commit()

    roster = build_roster(db_session, normal_user.id)
    entry = next(e for e in roster.entries if e.name == "Priya Patel")
    assert entry.file_count == 2
    assert entry.profile_id == profile.id


def test_roster_excludes_unknown_speaker_labels(db_session, normal_user):
    """`canonical_speaker_label` only resolves to the UNKNOWN sentinel when the
    raw diarization name itself is literally "Unknown" — an unlabeled speaker
    whose raw name is a real diarization slot (e.g. "SPEAKER_00") legitimately
    surfaces under that raw name, which is correct and NOT what this guards."""
    media = _make_file(db_session, normal_user)
    _add_speaker(db_session, media, normal_user, display_name=None, name="Unknown")

    roster = build_roster(db_session, normal_user.id)
    assert roster.entries == ()


def test_roster_distinct_cap_declines_a_pathologically_large_roster(
    db_session, normal_user, monkeypatch
):
    """Exercise the REAL decline branch in `build_roster` — the cap is
    monkeypatched down to 2 rather than creating 500+ real rows (too slow for
    the fast suite), but the grouping/decline code path is exactly the one
    a real oversized roster would hit."""
    import app.services.chat.speaker_resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, "ROSTER_DISTINCT_CAP", 2)
    media = _make_file(db_session, normal_user)
    for i, display in enumerate(("Alice Chen", "Bob Ng", "Priya Patel")):
        _add_speaker(db_session, media, normal_user, display_name=display, name=f"SPEAKER_{i:02d}")

    roster = build_roster(db_session, normal_user.id)
    assert roster.declined is True
    assert roster.entries == ()

    result = resolve_speaker_mentions(
        db_session, "What did Alice Chen say?", user_id=normal_user.id
    )
    assert result.declined is True
    assert result.matched == ()
