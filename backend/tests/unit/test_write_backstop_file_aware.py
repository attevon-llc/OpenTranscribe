"""The OpenSearch write backstop re-reads the FILE's consent on every write (issue #1356)."""

from __future__ import annotations

import pytest

from app.services.opensearch_service import speaker_write
from app.services.privacy import policy
from app.services.privacy.consent import clear_voiceprint_consent_hooks
from tests.fixtures.privacy_switches import file_in


@pytest.fixture(autouse=True)
def _clean():
    policy.reset_privacy_ceiling_resolver()
    policy.clear_write_backstop_cache()
    clear_voiceprint_consent_hooks()
    yield
    clear_voiceprint_consent_hooks()
    policy.clear_write_backstop_cache()


def test_backstop_refuses_declined_file_even_when_the_subject_is_cached_allowed(
    db_session, normal_user
):
    mf = file_in(db_session, normal_user, None)
    assert policy.assert_voiceprint_write_allowed(
        normal_user.id, None, db=db_session, media_file_id=mf.id
    )
    # The subject answer is now cached as allowed. The opt-out must still bite at once.
    mf.voiceprint_consent = "declined"
    db_session.flush()

    assert not policy.assert_voiceprint_write_allowed(
        normal_user.id, None, db=db_session, media_file_id=mf.id
    )
    # Control: the same subject WITHOUT a file is still allowed (profile/cluster writes).
    assert policy.assert_voiceprint_write_allowed(normal_user.id, None, db=db_session)


def test_backstop_hooks_receive_the_file_id(db_session, normal_user):
    from app.services.privacy.consent import register_voiceprint_consent_hook

    seen: list[int | None] = []

    def hook(db, request):
        seen.append(request.media_file_id)
        return True

    register_voiceprint_consent_hook(hook)
    mf = file_in(db_session, normal_user, None)

    assert policy.assert_voiceprint_write_allowed(
        normal_user.id, None, db=db_session, media_file_id=mf.id
    )
    assert mf.id in seen


def test_bulk_write_skips_only_declined_entries(db_session, normal_user, monkeypatch):
    ok = file_in(db_session, normal_user, None)
    bad = file_in(db_session, normal_user, None)
    bad.voiceprint_consent = "declined"
    db_session.commit()

    class _Client:
        def __init__(self):
            self.bodies: list = []

        def bulk(self, body, **kw):
            self.bodies.append(body)
            return {"errors": False, "items": []}

    from contextlib import contextmanager

    @contextmanager
    def same_session():
        yield db_session

    # The writer opens its own short session; under the savepoint harness only the test's own
    # session can see the uncommitted rows.
    monkeypatch.setattr("app.db.session_utils.session_scope", same_session)
    client = _Client()
    monkeypatch.setattr(speaker_write._client, "opensearch_client", client)
    monkeypatch.setattr(speaker_write, "get_speaker_index_v4", lambda: "speakers_v4")

    def entry(speaker_id, media_file):
        return {
            "speaker_id": speaker_id,
            "speaker_uuid": f"00000000-0000-0000-0000-00000000000{speaker_id}",
            "user_id": normal_user.id,
            "name": "s",
            "embedding": [0.1] * 256,
            "media_file_id": media_file.id,
        }

    speaker_write.bulk_add_speaker_embeddings_v4([entry(1, ok), entry(2, bad)])

    written = [d for body in client.bodies for d in body if "index" not in d]
    assert [d["speaker_id"] for d in written] == [1]
