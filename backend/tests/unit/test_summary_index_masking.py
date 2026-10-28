"""The summary documents in the search index do not carry a hidden speaker name (#1280).

A stored summary is model output and quotes speaker names. It is not rewritten when a switch
changes (owner decision: disclosed instead), but the COPY that is indexed for search and chat is
rebuilt from it on every reindex, and must not put a name back into the index that the file's
policy hides. While the file's voiceprints are off, a name a voiceprint made is replaced by that
speaker's diarizer label in the indexed text; a typed name is untouched; with voiceprints on
nothing changes.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from app.services.ingest_artifacts.index_mapping import build_summary_documents
from app.services.ingest_artifacts.index_mapping import mask_names
from app.services.ingest_artifacts.service import hidden_speaker_names
from app.services.privacy import policy
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.provenance_world import ALICE
from tests.fixtures.provenance_world import BOB
from tests.fixtures.provenance_world import CAROL
from tests.fixtures.provenance_world import build_world

SUMMARY: dict[str, Any] = {
    "bluf": f"{ALICE} and {BOB} agreed on the plan.",
    "action_items": [{"text": f"{ALICE.upper()} sends the notes to {CAROL}."}],
    "metadata": {"model": "m"},
}


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


def test_mask_names_is_whole_word_and_case_insensitive():
    masks = {"Alice Zylofenix": "SPEAKER_00"}

    assert mask_names("alice zylofenix spoke", masks) == "SPEAKER_00 spoke"
    assert mask_names("ALICE ZYLOFENIX.", masks) == "SPEAKER_00."
    assert mask_names("Alice Zylofenixes spoke", masks) == "Alice Zylofenixes spoke"
    assert mask_names("Malice Zylofenix spoke", masks) == "Malice Zylofenix spoke"
    assert mask_names("Bob spoke", masks) == "Bob spoke"
    assert mask_names("nothing to mask", {}) == "nothing to mask"


def test_mask_names_replaces_the_longer_name_whole_and_never_rescans():
    masks = {"Ann": "SPEAKER_01", "Ann Lee": "SPEAKER_00", "SPEAKER_00": "SPEAKER_09"}

    assert mask_names("Ann Lee met Ann", masks) == "SPEAKER_00 met SPEAKER_01"


def test_mask_names_ignores_regex_metacharacters_in_a_name():
    assert mask_names("Dr. (A+B) spoke", {"Dr. (A+B)": "SPEAKER_00"}) == "SPEAKER_00 spoke"


def test_summary_documents_mask_the_content_and_the_embedded_text():
    documents = build_summary_documents(
        file_uuid="u",
        file_id=1,
        summary_data=SUMMARY,
        facts={"roster": ["SPEAKER_00", BOB]},
        base_metadata={"title": "t"},
        name_masks={ALICE: "SPEAKER_00"},
    )

    assert documents, "the summary produced no documents"
    for document in documents:
        assert ALICE.lower() not in document["content"].lower()
        assert ALICE.lower() not in document["embedding_text"].lower()
    contents = " ".join(d["content"] for d in documents)
    assert "SPEAKER_00 and" in contents
    assert BOB in contents
    assert CAROL in contents


def test_summary_documents_are_unchanged_without_masks():
    documents = build_summary_documents(
        file_uuid="u",
        file_id=1,
        summary_data=SUMMARY,
        facts={"roster": []},
        base_metadata={"title": "t"},
    )

    assert ALICE in " ".join(d["content"] for d in documents)


def test_hidden_names_are_the_voiceprint_made_ones_of_an_off_file(db_session, normal_user):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    )

    assert hidden_speaker_names(db_session, world.file.id) == {ALICE: "SPEAKER_00"}


def test_no_names_are_hidden_while_voiceprints_are_on(db_session, normal_user):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    assert hidden_speaker_names(db_session, world.file.id) == {}


def test_a_name_another_speaker_visibly_carries_is_not_masked(db_session, normal_user):
    """A hidden "Alice" must not strip a typed "Alice" of the same file."""
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    )
    world.bob.display_name = ALICE

    assert hidden_speaker_names(db_session, world.file.id) == {}


def test_a_hidden_voice_suggestion_is_masked_too(db_session, normal_user):
    from tests.fixtures.speaker_cleanup_seams import make_file
    from tests.fixtures.speaker_cleanup_seams import make_speaker

    org = make_org(db_session, voiceprints=False)
    media_file = make_file(db_session, normal_user)
    media_file.organization_id = org.id
    make_speaker(
        db_session,
        normal_user,
        media_file,
        suggested_name="Ophelia Marchbanks",
        suggestion_source="voice_match",
        confidence=0.9,
    )

    assert hidden_speaker_names(db_session, media_file.id) == {"Ophelia Marchbanks": "SPEAKER_00"}


@pytest.fixture
def summary_plane(monkeypatch, db_session):
    """Run ``_index_summary_plane``; return the documents it would bulk-write."""
    from app.services.search.indexing_service import TranscriptIndexingService

    written: list[dict[str, Any]] = []
    service = TranscriptIndexingService()

    @contextlib.contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", scope)

    def bulk(pairs, use_neural):
        written.extend(doc for _, doc in pairs)
        return len(pairs)

    monkeypatch.setattr(service, "_bulk_index_documents", bulk)
    monkeypatch.setattr(service, "_prune_stale_summary_leaves", lambda *a, **k: 0)

    def run(world) -> list[dict[str, Any]]:
        written.clear()
        world.file.summary_data = SUMMARY
        db_session.flush()
        service._index_summary_plane(
            file_id=world.file.id,
            file_uuid=str(world.file.uuid),
            base_metadata={"title": "t"},
            use_neural=False,
        )
        return list(written)

    return run


def test_an_off_files_indexed_summary_says_the_diarizer_label(
    db_session, normal_user, summary_plane
):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    )

    contents = " ".join(d["content"] for d in summary_plane(world))

    assert ALICE.lower() not in contents.lower()
    assert "SPEAKER_00" in contents
    assert BOB in contents


def test_an_on_files_indexed_summary_keeps_the_name(db_session, normal_user, summary_plane):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    contents = " ".join(d["content"] for d in summary_plane(world))

    assert ALICE in contents


def test_the_stored_summary_is_not_rewritten(db_session, normal_user, summary_plane):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    )

    summary_plane(world)
    db_session.refresh(world.file)

    assert world.file.summary_data["bluf"] == SUMMARY["bluf"]


def test_an_unreadable_policy_masks_the_voiceprint_name(db_session, normal_user, monkeypatch):
    """Fail closed: when the decision cannot be made, the indexed summary hides the name."""
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    def broken(db, file_id):
        raise RuntimeError("policy store unreachable")

    monkeypatch.setattr(policy, "voiceprints_allowed_for_file", broken)

    assert hidden_speaker_names(db_session, world.file.id) == {ALICE: "SPEAKER_00"}
