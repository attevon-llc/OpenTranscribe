"""Speaker profile sync: chunked, batched, no transaction across the network, and convergent (#1337 P10).

The sync used to load every speaker and issue one OpenSearch ``exists`` per row inside a single
database transaction (580,000 calls, 15 minutes, past the idle-in-transaction timeout). It now
reads id-keyed chunks, ends the transaction, then uses one ``mget`` and one bulk request per chunk.
A recording stand-in for the OpenSearch client is used so the call pattern is observable without
a cluster; the real-cluster behaviour stays covered by ``test_speaker_metadata.py``.
"""

from __future__ import annotations

import json
import types
import uuid as uuid_pkg

import pytest

from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.services.opensearch_service import client as os_client
from app.services.opensearch_service import speaker_metadata

pytestmark = pytest.mark.unit


class _RecordingClient:
    """Just enough of the OpenSearch client for mget and the bulk helper."""

    def __init__(self, documents: dict[str, dict], events: list[str]) -> None:
        self.documents = documents
        self.events = events
        self.mgets = 0
        self.bulks = 0
        self.exists_calls = 0
        self.updates = 0
        # The bulk helper serialises through the client's transport.
        self.transport = types.SimpleNamespace(
            serializer=types.SimpleNamespace(dumps=json.dumps, mimetype="application/json")
        )

    def exists(self, **_: object) -> bool:
        self.exists_calls += 1
        return True

    def mget(self, index, body, **_):
        self.mgets += 1
        self.events.append("mget")
        return {
            "docs": [
                {"_id": i, "found": True, "_source": dict(self.documents[i])}
                if i in self.documents
                else {"_id": i, "found": False}
                for i in body["ids"]
            ]
        }

    def bulk(self, body, **_):
        self.bulks += 1
        self.events.append("bulk")
        lines = [json.loads(line) for line in body.splitlines() if line]
        items = []
        for action, data in zip(lines[0::2], lines[1::2], strict=True):
            doc_id = action["update"]["_id"]
            self.documents[doc_id].update(data["doc"])
            self.updates += 1
            items.append({"update": {"_id": doc_id, "status": 200}})
        return {"errors": False, "items": items}


def _seed(db, user, n):
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="s.mp4",
        content_type="video/mp4",
        file_size=1,
        storage_path=f"test/{uuid_pkg.uuid4().hex}.mp4",
    )
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=user.id, name="P")
    db.add_all([media, profile])
    db.flush()
    speakers = [
        Speaker(
            uuid=uuid_pkg.uuid4(),
            user_id=user.id,
            media_file_id=media.id,
            name=f"SPEAKER_{i:02d}",
            display_name=f"Person {i}",
            profile_id=profile.id,
            verified=True,
        )
        for i in range(n)
    ]
    db.add_all(speakers)
    db.commit()
    return profile, speakers


def test_sync_uses_mget_and_bulk_per_chunk_and_converges(db_session, normal_user, monkeypatch):
    profile, speakers = _seed(db_session, normal_user, 7)
    events: list[str] = []
    stale: dict[str, object] = {
        "profile_id": None,
        "profile_uuid": None,
        "display_name": None,
        "verified": False,
    }
    documents = {str(s.uuid): dict(stale) for s in speakers[:6]}  # the 7th has no document
    fake = _RecordingClient(documents, events)
    monkeypatch.setattr(os_client, "opensearch_client", fake)
    monkeypatch.setattr(speaker_metadata, "SYNC_CHUNK_SIZE", 3, raising=False)

    # Count the other tests' committed rows out: the sweep is global over the shared database.
    others = db_session.query(Speaker).filter(Speaker.id.notin_([s.id for s in speakers])).count()
    first = speaker_metadata.sync_speaker_profiles_to_opensearch(db_session)

    assert fake.exists_calls == 0  # no per-row HEAD requests
    assert fake.mgets == -(-(7 + others) // 3)  # one mget per chunk of 3
    assert first["updated"] == 6 and first["errors"] == 0
    assert first["skipped"] >= 1  # control: the speaker with no document was reported
    doc = documents[str(speakers[0].uuid)]
    assert (doc["profile_uuid"], doc["display_name"], doc["verified"]) == (
        str(profile.uuid),
        "Person 0",
        True,
    )

    second = speaker_metadata.sync_speaker_profiles_to_opensearch(db_session)
    assert second["updated"] == 0 and fake.updates == 6  # converged: nothing left to write


def test_sync_ends_the_transaction_before_each_network_call(db_session, normal_user, monkeypatch):
    _seed(db_session, normal_user, 4)
    events: list[str] = []
    fake = _RecordingClient({}, events)
    monkeypatch.setattr(os_client, "opensearch_client", fake)
    monkeypatch.setattr(speaker_metadata, "SYNC_CHUNK_SIZE", 2, raising=False)
    real_commit = db_session.commit

    def recording_commit() -> None:
        events.append("commit")
        real_commit()

    monkeypatch.setattr(db_session, "commit", recording_commit)

    speaker_metadata.sync_speaker_profiles_to_opensearch(db_session)

    assert "mget" in events  # control: the network was reached
    for position, event in enumerate(events):
        if event == "mget":
            assert events[position - 1] == "commit", events
