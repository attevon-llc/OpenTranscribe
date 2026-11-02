"""The manual batch-matching script reads a speaker's voiceprint by the speaker's UUID.

OpenSearch speaker documents are keyed by the speaker UUID. The script used to pass the integer
primary key, so every lookup missed and the script found no matches at all.
"""

from __future__ import annotations

import importlib

import pytest

from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


def test_the_batch_script_looks_a_voiceprint_up_by_speaker_uuid(
    db_session, normal_user, monkeypatch
):
    script = importlib.import_module("app.scripts.batch_speaker_matching")
    org = make_org(db_session, voiceprints=True)
    speaker = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org))
    db_session.commit()
    looked_up: list[object] = []

    class Session:
        """The test's session with ``close`` disarmed: the script closes what it opens."""

        def __getattr__(self, name):
            return getattr(db_session, name)

        def close(self):
            return None

    def fake_embedding(key):
        looked_up.append(key)
        return None

    monkeypatch.setattr(script, "get_db", lambda: iter([Session()]))
    monkeypatch.setattr(script, "SpeakerEmbeddingService", lambda: None)
    monkeypatch.setattr(script, "SpeakerMatchingService", lambda db, svc: None)
    monkeypatch.setattr(script, "get_speaker_embedding", fake_embedding)

    script.batch_process_speaker_matches()

    assert str(speaker.uuid) in [str(k) for k in looked_up]
    assert speaker.id not in looked_up
