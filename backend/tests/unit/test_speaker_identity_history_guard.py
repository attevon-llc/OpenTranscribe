"""The speaker identity history is append-only in the database (issue #1336, v462 guard).

Every refusal is checked by SQLSTATE 42501 inside a savepoint, beside a control that the same
kind of statement passes when the guard's own exception applies (the speaker's cascade, the
user deletion's SET NULL, a voiceprint purge).
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fixtures.speaker_cleanup_seams import make_file
from fixtures.speaker_cleanup_seams import make_speaker
from sqlalchemy import text

from tests.unit._audit_rows import assert_refused


def _event(conn, speaker_id: int, *, voiceprint: bool = False, actor: int | None = None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO speaker_identity_event (uuid, speaker_id, kind, actor_kind, "
                "actor_user_id, voiceprint, after_value) VALUES (:u, :s, 'rename', :k, :a, :v, "
                'CAST(\'{"name": "Bob"}\' AS jsonb)) RETURNING id'
            ),
            {
                "u": uuid_pkg.uuid4(),
                "s": speaker_id,
                "k": "user" if actor else "system",
                "a": actor,
                "v": voiceprint,
            },
        ).scalar_one()
    )


def _exists(conn, event_id: int) -> bool:
    return bool(
        conn.execute(
            text("SELECT EXISTS(SELECT 1 FROM speaker_identity_event WHERE id = :i)"),
            {"i": event_id},
        ).scalar()
    )


@pytest.fixture
def world(db_session, normal_user):
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    return db_session.connection(), speaker


def test_an_update_of_a_recorded_value_is_refused(world):
    conn, speaker = world
    event_id = _event(conn, speaker.id)
    with assert_refused(conn):
        conn.execute(
            text("UPDATE speaker_identity_event SET kind = 'link' WHERE id = :i"), {"i": event_id}
        )
    with assert_refused(conn):
        conn.execute(
            text("UPDATE speaker_identity_event SET after_value = NULL WHERE id = :i"),
            {"i": event_id},
        )


def test_an_actor_can_only_be_cleared_and_the_user_deletion_does_so(
    db_session, world, normal_user, admin_user
):
    conn, speaker = world
    event_id = _event(conn, speaker.id, actor=admin_user.id)
    with assert_refused(conn):
        conn.execute(
            text("UPDATE speaker_identity_event SET actor_user_id = :o WHERE id = :i"),
            {"o": normal_user.id, "i": event_id},
        )
    conn.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": admin_user.id})
    row = conn.execute(
        text("SELECT actor_user_id, actor_kind FROM speaker_identity_event WHERE id = :i"),
        {"i": event_id},
    ).one()
    assert tuple(row) == (None, "user"), "de-attributed, still a person's change"


def test_a_direct_delete_is_refused_and_the_speakers_cascade_is_not(world):
    conn, speaker = world
    event_id = _event(conn, speaker.id)
    with assert_refused(conn):
        conn.execute(text("DELETE FROM speaker_identity_event WHERE id = :i"), {"i": event_id})
    assert _exists(conn, event_id)
    conn.execute(text("DELETE FROM speaker WHERE id = :s"), {"s": speaker.id})
    assert not _exists(conn, event_id), "the history goes with the speaker"


def test_only_a_voiceprint_row_goes_and_only_under_the_purge_switch(world):
    conn, speaker = world
    machine = _event(conn, speaker.id, voiceprint=True)
    person = _event(conn, speaker.id)
    with assert_refused(conn):  # control: no switch
        conn.execute(text("DELETE FROM speaker_identity_event WHERE id = :i"), {"i": machine})
    nested = conn.begin_nested()
    conn.execute(text("SELECT set_config('opentranscribe.voiceprint_purge', 'on', true)"))
    conn.execute(text("DELETE FROM speaker_identity_event WHERE id = :i"), {"i": machine})
    with assert_refused(conn):
        conn.execute(text("DELETE FROM speaker_identity_event WHERE id = :i"), {"i": person})
    conn.execute(text("SELECT set_config('opentranscribe.voiceprint_purge', '', true)"))
    nested.commit()
    assert (_exists(conn, machine), _exists(conn, person)) == (False, True)


def test_truncate_is_refused(world):
    conn, speaker = world
    _event(conn, speaker.id)
    with assert_refused(conn):
        conn.execute(text("TRUNCATE speaker_identity_event"))


def test_the_checks_refuse_an_unknown_kind_and_a_system_row_naming_a_person(world, normal_user):
    from sqlalchemy.exc import IntegrityError

    conn, speaker = world
    for kind, actor_kind, actor in (("renamed", "system", None), ("rename", "system", 1)):
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                text(
                    "INSERT INTO speaker_identity_event (uuid, speaker_id, kind, actor_kind, "
                    "actor_user_id) VALUES (:u, :s, :k, :ak, :a)"
                ),
                {
                    "u": uuid_pkg.uuid4(),
                    "s": speaker.id,
                    "k": kind,
                    "ak": actor_kind,
                    "a": actor and normal_user.id,
                },
            )
