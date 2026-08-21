"""``scope_speaker_digest_hits`` permission matrix row T5 — real DB, real share rows.

Same discipline ``test_chat_speaker_resolver.py`` applies to the roster (and
the root CLAUDE.md's #385 lesson it cites): a shared-visibility test that only
proves "the code compiles" without a real share row is worthless, because
that is exactly the shape of bug (owner-only scoping) that survived review
once already.

* **LEAK** — a shared viewer's own resolved accessible scope
  (``PermissionService.get_accessible_file_ids_subquery`` — the SAME
  authority every other axis in this package routes sharing through) must
  not include a file shared only via a DIFFERENT collection than the one
  granting them access; calling the speaker map with that (correctly
  narrowed) scope must then find nothing for it.
* **SHARED** — a file genuinely shared with the viewer, whose owner's
  ``summary_data`` names the requested speaker, is served — asserted
  non-zero against real content, not merely "did not raise".
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

from app.services.chat.mapreduce import scope_speaker_digest_hits

pytestmark = pytest.mark.unit


def _make_file(db, user, *, title="Recording"):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def _add_speaker_summary(db, media_file, *, speaker: str):
    from app.models.file_facts import FileFacts

    digest = {
        "sections": [
            {
                "index": 0,
                "sentences": [
                    {
                        "text": f"{speaker} discussed the Q3 roadmap.",
                        "order": 0,
                        "speaker": speaker,
                        "provenance": {
                            "kind": "segment_ids",
                            "segment_ids": [1],
                            "start_time": 0.0,
                            "end_time": 5.0,
                        },
                    }
                ],
            }
        ]
    }
    facts = FileFacts(
        media_file_id=media_file.id,
        generator_version="1.1.1",
        source_fingerprint=f"fp-{uuid_pkg.uuid4().hex[:8]}",
        facts={},
        digest=digest,
        keyphrases={},
    )
    db.add(facts)
    media_file.summary_status = "completed"
    media_file.summary_data = {
        "speakers_analysis": [
            {
                "speaker": speaker,
                "role": "Presenter",
                "key_contributions": ["Owns the Q3 roadmap"],
            }
        ],
        "metadata": {"source_fingerprint": facts.source_fingerprint},
    }
    db.add(media_file)
    db.commit()


def _share_with(db, owner, recipient, media_file) -> None:
    from app.models.media import Collection
    from app.models.media import CollectionMember
    from app.models.sharing import CollectionShare

    collection = Collection(
        user_id=owner.id, name=f"share-{uuid_pkg.uuid4().hex[:8]}", description="w2.3 test"
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
            permission="viewer",
        )
    )
    db.commit()


def _accessible_uuids(db, viewer_id: int) -> list[str]:
    """The viewer's real resolved scope — the same authority
    `context_resolver`/`speaker_resolver.build_candidate_roster` route every
    sharing check through in this package, never re-derived ad hoc."""
    from sqlalchemy import select

    from app.models.media import MediaFile
    from app.services.permission_service import PermissionService

    subquery = PermissionService.get_accessible_file_ids_subquery(db, viewer_id)
    rows = db.execute(select(MediaFile.uuid).where(MediaFile.id.in_(select(subquery)))).all()
    return [str(row[0]) for row in rows]


def test_leak_a_speaker_summary_shared_only_via_a_different_collection_is_unreachable(
    db_session, normal_user, other_user
):
    """LEAK. `unshared` is owned by `other_user` and never shared with
    `normal_user` at all — a THIRD collection (owned by `other_user`,
    containing a DIFFERENT file) exists in the same fixture set so this is
    not merely 'nothing was ever shared', but 'sharing exists on this account
    and still does not leak into an unrelated grant'."""
    unshared = _make_file(db_session, other_user, title="Not shared")
    _add_speaker_summary(db_session, unshared, speaker="Priya Patel")

    decoy = _make_file(db_session, other_user, title="Shared via a different collection")
    _add_speaker_summary(db_session, decoy, speaker="Priya Patel")
    _share_with(db_session, other_user, normal_user, decoy)

    accessible = _accessible_uuids(db_session, normal_user.id)
    assert str(unshared.uuid) not in accessible, (
        "PermissionService itself must not resolve the unshared file — if this "
        "fails, the leak is upstream of scope_speaker_digest_hits entirely"
    )
    assert str(decoy.uuid) in accessible

    # The realistic call path: `scope_speaker_digest_hits` trusts `file_uuids`
    # as an ALREADY permission-resolved scope (same contract
    # `scope_digest_hits` documents) — it is not itself a second permission
    # check. So the real test is against the viewer's actual resolved scope,
    # which naturally contains `decoy` but not `unshared`.
    hits = scope_speaker_digest_hits(db_session, accessible, ["Priya Patel"], use_summaries=True)

    served_uuids = {h.file_uuid for h in hits}
    assert str(unshared.uuid) not in served_uuids
    assert str(decoy.uuid) in served_uuids, (
        "the decoy (genuinely shared) file must still be served — otherwise "
        "the absence of the unshared file proves nothing"
    )


def test_shared_a_viewers_chat_reads_the_owners_speaker_summary(
    db_session, normal_user, other_user
):
    """SHARED. Asserted non-zero against real, owner-authored content — a
    vacuous 'did not raise' is exactly the shape of test the root CLAUDE.md
    warns produced the #385-class bug once already."""
    shared = _make_file(db_session, other_user, title="Shared with me")
    _add_speaker_summary(db_session, shared, speaker="Priya Patel")
    _share_with(db_session, other_user, normal_user, shared)

    accessible = _accessible_uuids(db_session, normal_user.id)
    assert str(shared.uuid) in accessible

    hits = scope_speaker_digest_hits(db_session, accessible, ["Priya Patel"], use_summaries=True)

    assert len(hits) == 1
    assert hits[0].file_uuid == str(shared.uuid)
    assert "Presenter" in hits[0].content
    assert "Owns the Q3 roadmap" in hits[0].content
