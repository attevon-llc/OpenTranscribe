"""The manual batch-matching script writes nothing for a speaker of a held or quarantined file.

A legal hold or a quarantine freezes the file's speakers as they stand (``hold_guard``): the
script must not look their voiceprints up, store match records for them or write a name
suggestion, and it reports how many it left out. A hold placed while the script runs (it walks
every speaker) is seen before the speaker is matched.
"""

from __future__ import annotations

import importlib

import pytest
from sqlalchemy import text

from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


def _protect(db, file_id: int, how: str) -> None:
    db.execute(text(f"UPDATE media_file SET {how} = true WHERE id = :i"), {"i": file_id})
    db.flush()


@pytest.fixture()
def run(db_session, monkeypatch):
    script = importlib.import_module("app.scripts.batch_speaker_matching")
    looked_up: list[str] = []

    class Session:
        """The test's session with ``close`` disarmed: the script closes what it opens."""

        def __getattr__(self, name):
            return getattr(db_session, name)

        def close(self):
            return None

    def fake_embedding(key):
        looked_up.append(str(key))
        return None

    monkeypatch.setattr(script, "get_db", lambda: iter([Session()]))
    monkeypatch.setattr(script, "SpeakerEmbeddingService", lambda: None)
    monkeypatch.setattr(script, "SpeakerMatchingService", lambda db, svc: None)
    monkeypatch.setattr(script, "get_speaker_embedding", fake_embedding)
    return script, looked_up


def test_speakers_of_held_and_quarantined_files_are_left_out_and_counted(
    db_session, normal_user, run
):
    script, looked_up = run
    org = make_org(db_session, voiceprints=True)
    free = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org))
    held = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org))
    quarantined = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org))
    _protect(db_session, held.media_file_id, "legal_hold")
    _protect(db_session, quarantined.media_file_id, "is_quarantined")
    db_session.commit()

    speakers, skipped = script.speakers_to_match(db_session)
    script.batch_process_speaker_matches()

    ids = {s.id for s in speakers}
    assert free.id in ids and held.id not in ids and quarantined.id not in ids
    assert skipped >= 2
    assert str(free.uuid) in looked_up
    assert str(held.uuid) not in looked_up and str(quarantined.uuid) not in looked_up


def test_a_hold_placed_during_the_run_is_seen_before_the_speaker_is_matched(
    db_session, normal_user, run, monkeypatch
):
    script, looked_up = run
    org = make_org(db_session, voiceprints=True)
    speaker = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org))
    db_session.commit()
    planned = script.speakers_to_match

    def plan_then_hold(db):
        result = planned(db)
        _protect(db_session, speaker.media_file_id, "legal_hold")
        return result

    monkeypatch.setattr(script, "speakers_to_match", plan_then_hold)
    script.batch_process_speaker_matches()

    assert str(speaker.uuid) not in looked_up
