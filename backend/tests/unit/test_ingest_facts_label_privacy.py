"""Facts, digest and their fingerprint follow the file's voiceprint switch (#1280).

``generate_file_artifacts`` persists a speaker roster and a digest that quote speaker names.
While the file's voiceprints are off a name a voiceprint wrote is not in either, and turning
the switch back on regenerates them with the names (the fingerprint changes with the label).
"""

from __future__ import annotations

import json

import pytest

from app.services.ingest_artifacts.service import generate_file_artifacts
from app.services.ingest_artifacts.service import load_ordered_segments
from app.services.privacy import policy
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


def _stored(row) -> str:
    return json.dumps({"facts": row.facts, "digest": row.digest}, sort_keys=True)


def test_an_off_files_facts_and_digest_carry_no_voiceprint_name(db_session, normal_user):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    )

    row = generate_file_artifacts(db_session, world.file.id)

    assert row is not None
    stored = _stored(row)
    assert ALICE not in stored
    assert "SPEAKER_00" in row.facts["roster"]
    assert CAROL in row.facts["roster"]
    assert BOB in row.facts["roster"]


def test_an_on_files_facts_carry_every_name(db_session, normal_user):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    row = generate_file_artifacts(db_session, world.file.id)

    assert row is not None
    assert {ALICE, CAROL, BOB} <= set(row.facts["roster"])


def test_flipping_the_switch_regenerates_the_artifacts_in_both_directions(db_session, normal_user):
    world = build_world(db_session, normal_user)

    on = generate_file_artifacts(db_session, world.file.id)
    assert on is not None and ALICE in on.facts["roster"]
    on_fingerprint = on.source_fingerprint

    set_instance_switch(db_session, "voiceprints", False)
    off = generate_file_artifacts(db_session, world.file.id)
    assert off is not None
    assert ALICE not in _stored(off)
    assert off.source_fingerprint != on_fingerprint

    set_instance_switch(db_session, "voiceprints", True)
    back = generate_file_artifacts(db_session, world.file.id)
    assert back is not None
    assert ALICE in back.facts["roster"]
    assert back.source_fingerprint == on_fingerprint


def test_an_unreadable_policy_keeps_a_voiceprint_name_out_of_the_segments(
    db_session, normal_user, monkeypatch
):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    def broken(db, file_id):
        raise RuntimeError("policy store unreachable")

    monkeypatch.setattr(policy, "voiceprints_allowed_for_file", broken)

    labels = {s["speaker"] for s in load_ordered_segments(db_session, world.file.id)}

    assert labels == {"SPEAKER_00", CAROL, BOB}
