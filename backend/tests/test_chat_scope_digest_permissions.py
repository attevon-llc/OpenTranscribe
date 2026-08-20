"""Permission-plane tests for the map-reduce scope map (W2.1, permission row T2).

``scope_digest_hits`` trusts its ``file_uuids`` argument as an already-resolved,
permission-checked scope — see its own docstring in
``services/chat/mapreduce.py``. It does no sharing logic of its own, so a
genuine leak/coverage test at this layer has to prove the SQL predicate really
restricts to that list against a REAL database, not a mock pre-configured to
already agree with the assertion:

- the LEAK half creates two real rows in Postgres and proves the exclusion is
  the ``.filter(MediaFile.uuid.in_(...))`` clause actually filtering, not a
  fixture that never put the excluded file in the digest at all;
- the SHARED half resolves ``file_uuids`` through the REAL sharing machinery
  (``context_resolver.resolve_scope_file_uuids`` over a real ``CollectionShare``
  / ``UserGroup``, the same one production uses) rather than hand-listing a
  uuid the fixture never actually shared — a "shared" test that never shares
  anything is worthless, per the review brief.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.api.deps_context import RequestContext
from app.models.file_facts import FileFacts
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.sharing import CollectionShare
from app.schemas.chat import ChatScope
from app.services.chat.context_resolver import resolve_scope_file_uuids
from app.services.chat.mapreduce import build_file_summaries
from app.services.chat.mapreduce import build_overview
from app.services.chat.mapreduce import scope_digest_hits


def _ctx(user, org_id=None) -> RequestContext:
    return RequestContext(user=user, org_id=org_id)


def _file(db, user, *, title="Recording"):
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def _facts(db, media, *, text="the discussion covered several items"):
    row = FileFacts(
        media_file_id=media.id,
        generator_version="1.1.1",
        source_fingerprint="fp",
        language="en",
        facts={},
        keyphrases={},
        digest={"sections": [{"index": 0, "text": text, "start_time": 0.0}]},
        digest_word_count=len(text.split()),
        section_count=1,
    )
    db.add(row)
    db.commit()
    return row


def _overview_for(db, file_uuids):
    hits = scope_digest_hits(db, file_uuids)
    summaries = build_file_summaries(db, hits, masked_text={id(h): h.content for h in hits})
    overview = build_overview("summarize this scope", summaries, files_in_scope=len(file_uuids))
    return hits, overview


def _share_collection(db, collection, *, owner, with_user=None, with_group=None):
    db.add(
        CollectionShare(
            uuid=uuid_pkg.uuid4(),
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user" if with_user is not None else "group",
            target_user_id=with_user.id if with_user is not None else None,
            target_group_id=with_group.id if with_group is not None else None,
            permission="viewer",
        )
    )
    db.commit()


def _collection_with(db, owner, media, *, name="Shared"):
    collection = Collection(uuid=uuid_pkg.uuid4(), name=name, user_id=owner.id)
    db.add(collection)
    db.commit()
    db.add(CollectionMember(collection_id=collection.id, media_file_id=media.id))
    db.commit()
    return collection


# ------------------------------------------------------------------------ LEAK


def test_a_file_outside_the_resolved_scope_never_appears_in_the_overview(db_session, normal_user):
    """Both files are REAL rows with real digests; only one is IN the scope
    passed to `scope_digest_hits`. A mock pre-configured to return only the
    in-scope row would pass this trivially — this hits real Postgres, so the
    exclusion proves the SQL predicate is what filters, not the fixture."""
    in_scope = _file(db_session, normal_user, title="In Scope Meeting")
    excluded = _file(db_session, normal_user, title="Secret Excluded Meeting")
    _facts(db_session, in_scope, text="the roadmap for the quarter")
    _facts(db_session, excluded, text="the secret excluded content")

    _hits, overview = _overview_for(db_session, [str(in_scope.uuid)])

    assert "In Scope Meeting" in overview.block
    assert "Secret Excluded Meeting" not in overview.block
    assert "secret excluded content" not in overview.block


def test_scope_digest_hits_matches_only_the_given_uuids_even_when_more_exist(
    db_session, normal_user
):
    kept = _file(db_session, normal_user, title="Kept")
    dropped = _file(db_session, normal_user, title="Dropped")
    _facts(db_session, kept)
    _facts(db_session, dropped)

    hits = scope_digest_hits(db_session, [str(kept.uuid)])

    assert {h.file_uuid for h in hits} == {str(kept.uuid)}


# ---------------------------------------------------------------------- SHARED


def test_a_collection_shared_files_digest_is_covered_by_the_map(
    db_session, normal_user, other_user
):
    """Resolves through the REAL sharing machinery (`context_resolver`), not a
    hand-listed uuid — a fixture that never actually shared anything would
    make this pass vacuously."""
    theirs = _file(db_session, other_user, title="Q3 Budget Review")
    _facts(db_session, theirs, text="budget numbers were reviewed line by line")
    collection = _collection_with(db_session, other_user, theirs, name="Finance")
    _share_collection(db_session, collection, owner=other_user, with_user=normal_user)

    resolved = resolve_scope_file_uuids(
        db_session, _ctx(normal_user), ChatScope(collection_uuids=[str(collection.uuid)])
    )
    assert resolved == [str(theirs.uuid)], "the share must actually resolve or this test is vacuous"

    _hits, overview = _overview_for(db_session, resolved)

    assert "Q3 Budget Review" in overview.block


def test_a_group_shared_files_digest_is_covered_by_the_map(db_session, normal_user, other_user):
    from app.models.group import UserGroup
    from app.models.group import UserGroupMember

    group = UserGroup(uuid=uuid_pkg.uuid4(), name="Team", owner_id=other_user.id)
    db_session.add(group)
    db_session.commit()
    db_session.add(
        UserGroupMember(uuid=uuid_pkg.uuid4(), group_id=group.id, user_id=normal_user.id)
    )
    db_session.commit()

    theirs = _file(db_session, other_user, title="Group Standup")
    _facts(db_session, theirs, text="standup notes from the whole team")
    collection = _collection_with(db_session, other_user, theirs, name="Team Docs")
    _share_collection(db_session, collection, owner=other_user, with_group=group)

    resolved = resolve_scope_file_uuids(
        db_session, _ctx(normal_user), ChatScope(collection_uuids=[str(collection.uuid)])
    )
    assert resolved == [str(theirs.uuid)], (
        "the group share must actually resolve or this test is vacuous"
    )

    _hits, overview = _overview_for(db_session, resolved)

    assert "Group Standup" in overview.block


def test_a_shared_file_with_no_digest_counts_rather_than_silently_dropping(
    db_session, normal_user, other_user
):
    """A shared file with no `file_facts` row at all (never ingest-processed)
    must be COUNTED as a gap, never simply absent with no signal. Mixed with a
    shared file that DOES have a digest, so this is the "some covered, some
    not" case — the all-missing case would leave `overview.block` empty for an
    unrelated reason (`CodeComposer.reduce` returns nothing at all when there
    are zero summaries) and would prove nothing about counting."""
    enriched = _file(db_session, other_user, title="Has A Digest")
    _facts(db_session, enriched, text="notes that were actually written down")
    never_enriched = _file(db_session, other_user, title="Never Enriched")
    collection = _collection_with(db_session, other_user, enriched, name="Team Docs")
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=never_enriched.id))
    db_session.commit()
    _share_collection(db_session, collection, owner=other_user, with_user=normal_user)

    resolved = resolve_scope_file_uuids(
        db_session, _ctx(normal_user), ChatScope(collection_uuids=[str(collection.uuid)])
    )
    assert resolved is not None, "a non-empty collection scope must never resolve to None"
    assert set(resolved) == {str(enriched.uuid), str(never_enriched.uuid)}

    hits, overview = _overview_for(db_session, resolved)

    assert hits.coverage["files_without_artifacts"] == 1
    assert "Has A Digest" in overview.block
    assert "recordings summarised here: 1 of 2 in scope" in overview.block
    assert "the other 1 have no digest available" in overview.block
