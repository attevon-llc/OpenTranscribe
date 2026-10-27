"""LLM prompts and summary staleness follow the file's voiceprint switch (#1280).

The summarization prompt names each speaker and keys the per-speaker statistics by that name.
While the file's voiceprints are off a name a voiceprint wrote is not in the prompt or the
statistics; the fingerprint that decides whether a stored summary is stale is built from the
same labels, so flipping the switch marks the summary stale. The speaker-identification prompt
uses the raw diarizer labels only. Each case has an "on" control.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_pkg

import pytest

from app.services.privacy import policy
from app.utils.transcript_builders import build_full_transcript
from app.utils.transcript_builders import build_speaker_segments
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.provenance_world import ALICE
from tests.fixtures.provenance_world import BOB
from tests.fixtures.provenance_world import CAROL
from tests.fixtures.provenance_world import build_world


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


@pytest.fixture
def summarization_inputs(monkeypatch, db_session):
    from app.tasks import summarization

    @contextlib.contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(summarization, "session_scope", scope)
    monkeypatch.setattr(summarization, "resolve_llm_masking", lambda db, media_file: None)

    def load(world) -> dict:
        return summarization._load_summarization_inputs(
            str(world.file.uuid), str(uuid_pkg.uuid4()), force_regenerate=False
        )

    return load


def test_an_off_files_prompt_and_statistics_carry_no_voiceprint_name(
    db_session, normal_user, summarization_inputs
):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    )

    inputs = summarization_inputs(world)

    assert ALICE not in inputs["full_transcript"]
    assert "SPEAKER_00:" in inputs["full_transcript"]
    assert CAROL in inputs["full_transcript"]
    assert BOB in inputs["full_transcript"]
    assert ALICE not in inputs["speaker_stats"]
    assert "SPEAKER_00" in inputs["speaker_stats"]
    assert {CAROL, BOB} <= set(inputs["speaker_stats"])


def test_an_on_files_prompt_and_statistics_carry_every_name(
    db_session, normal_user, summarization_inputs
):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    inputs = summarization_inputs(world)

    assert f"{ALICE}:" in inputs["full_transcript"]
    assert {ALICE, CAROL, BOB} == set(inputs["speaker_stats"])


def test_flipping_the_switch_changes_the_fingerprint_that_marks_a_summary_stale(
    db_session, normal_user, summarization_inputs
):
    world = build_world(db_session, normal_user)

    before = summarization_inputs(world)["source_fingerprint"]
    set_instance_switch(db_session, "voiceprints", False)
    off = summarization_inputs(world)["source_fingerprint"]
    set_instance_switch(db_session, "voiceprints", True)
    back_on = summarization_inputs(world)["source_fingerprint"]

    assert off != before
    assert back_on == before


def test_the_speaker_id_prompt_builders_use_only_the_diarizer_label(db_session, normal_user):
    """``build_full_transcript`` / ``build_speaker_segments`` never print a display name."""
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    text = build_full_transcript(world.segments)
    segments = build_speaker_segments(world.segments)

    for name in (ALICE, CAROL, BOB):
        assert name not in text
        assert all(name not in str(s) for s in segments)
    assert "SPEAKER_00:" in text
    assert {s["speaker_label"] for s in segments} == {"SPEAKER_00", "SPEAKER_01", "SPEAKER_02"}


def test_an_unreadable_policy_keeps_a_voiceprint_name_out_of_the_prompt(
    db_session, normal_user, summarization_inputs, monkeypatch
):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    def broken(db, file_id):
        raise RuntimeError("policy store unreachable")

    monkeypatch.setattr(policy, "voiceprints_allowed_for_file", broken)

    inputs = summarization_inputs(world)

    assert ALICE not in inputs["full_transcript"]
    assert BOB in inputs["full_transcript"]
