"""Hiding a voice-made identity and restoring it changes nothing in the database (#1280, D6).

An auto-accepted voice match is stored exactly like a person's confirmation except for
``identity_source = 'voiceprint'``. Turning the instance's voiceprint switch off must hide it
from every surface that prints a name (file detail, the speaker list, an export, the chunk
index label) without touching the row, and turning it back on must show it again: the row's
identity columns are byte-identical before, during and after.
"""

from __future__ import annotations

import pytest
from fastapi import status
from sqlalchemy import text

from app.services.privacy import policy
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.provenance_world import ALICE
from tests.fixtures.provenance_world import build_world

pytestmark = pytest.mark.usefixtures("speaker_os_seams")

_IDENTITY_COLUMNS = (
    "display_name, verified, profile_id, identity_source, suggested_name, "
    "suggestion_source, confidence, resolved_display_name"
)


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


def _stored(db_session, speaker_id: int) -> tuple:
    db_session.expire_all()
    row = db_session.execute(
        text(f"SELECT {_IDENTITY_COLUMNS} FROM speaker WHERE id = :id"), {"id": speaker_id}
    ).one()
    return tuple(row)


def _surfaces(client, headers, world, db_session) -> dict[str, str]:
    """What each name-printing surface says about the voiceprint-named speaker."""
    from app.services.privacy.gates import voiceprints_allowed
    from app.tasks.search_indexing_task import resolve_chunk_speaker_name

    detail = client.get(f"/api/files/{world.file.uuid}", headers=headers)
    speakers = client.get("/api/speakers", headers=headers)
    export = client.get(f"/api/files/{world.file.uuid}/export?format=txt", headers=headers)
    for response in (detail, speakers, export):
        assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    indexed = resolve_chunk_speaker_name(
        world.alice, voiceprint_names=voiceprints_allowed(world.file.id, db_session)
    )
    return {
        "detail": detail.text,
        "speakers": speakers.text,
        "export": export.text,
        "index": indexed,
    }


def test_hide_then_restore_leaves_the_row_byte_identical(
    client, user_token_headers, db_session, normal_user
):
    world = build_world(db_session, normal_user)
    db_session.commit()
    before = _stored(db_session, world.alice.id)
    assert before[3] == "voiceprint" and before[0] == ALICE

    shown = _surfaces(client, user_token_headers, world, db_session)
    assert all(ALICE in surface for surface in shown.values()), shown.keys()

    set_instance_switch(db_session, "voiceprints", False)
    during = _stored(db_session, world.alice.id)
    hidden = _surfaces(client, user_token_headers, world, db_session)

    assert during == before
    for name, surface in hidden.items():
        assert ALICE not in surface, f"{name} still prints the hidden name"
    assert hidden["index"] == "SPEAKER_00"

    set_instance_switch(db_session, "voiceprints", True)
    restored = _surfaces(client, user_token_headers, world, db_session)

    assert _stored(db_session, world.alice.id) == before
    assert all(ALICE in surface for surface in restored.values())
