"""``account_purge``: an account delete reads and destroys THIS account's copies, all of them.

A HEAD-vs-split mutation comparison of ``services/file_cleanup_service.py`` (repo cleanup,
wave b10) found the account-scoped plan's tenant filters pinned by nothing: dropping the
``user_id`` filter from the speaker, profile, cluster or avatar read left every suite
green, and that mutant erases every OTHER account's voiceprints, profile embeddings,
centroids and avatars when one account is deleted. Every endpoint suite replaces
``purge_account_external_copies`` with a spy, so its centroid sweep, avatar deletes and
cache invalidation were unpinned too.

The plan read runs against real Postgres; the destroy is driven with its stores stubbed at
the names ``account_purge`` calls.
"""

import uuid as uuid_pkg

import pytest

from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerProfile
from app.services import account_purge


def _account_rows(db, user) -> dict[str, str]:
    """One file, speaker, profile (with avatar) and cluster owned by ``user``."""
    tag = uuid_pkg.uuid4().hex[:8]
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        filename=f"{tag}.wav",
        user_id=user.id,
        storage_path=f"user_{user.id}/{tag}.wav",
        thumbnail_path=f"thumbnails/{tag}.webp",
        playback_path=f"user_{user.id}/{tag}.playback.m4a",
        file_size=10,
        content_type="audio/wav",
    )
    db.add(media_file)
    db.flush()
    speaker = Speaker(uuid=uuid_pkg.uuid4(), name="S", user_id=user.id, media_file_id=media_file.id)
    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(), user_id=user.id, name=f"P {tag}", avatar_path=f"avatars/{tag}.png"
    )
    cluster = SpeakerCluster(uuid=uuid_pkg.uuid4(), user_id=user.id, label="c", member_count=0)
    db.add_all([speaker, profile, cluster])
    db.commit()
    return {
        "file_uuid": str(media_file.uuid),
        "storage_path": str(media_file.storage_path),
        "thumbnail_path": str(media_file.thumbnail_path),
        "playback_path": str(media_file.playback_path),
        "speaker_uuid": str(speaker.uuid),
        "profile_uuid": str(profile.uuid),
        "avatar_path": str(profile.avatar_path),
        "cluster_uuid": str(cluster.uuid),
    }


def test_the_account_plan_names_only_this_accounts_copies(db_session, normal_user, other_user):
    mine = _account_rows(db_session, normal_user)
    _account_rows(db_session, other_user)

    plan = account_purge.load_account_purge_plans(db_session, int(normal_user.id))

    assert plan.user_id == int(normal_user.id)
    assert [f["file_uuid"] for f in plan.files] == [mine["file_uuid"]]
    (file_plan,) = plan.files
    assert file_plan["owner_id"] == int(normal_user.id)
    for key in ("storage_path", "thumbnail_path", "playback_path"):
        assert file_plan[key] == mine[key], key
    assert plan.speaker_uuids == [mine["speaker_uuid"]]
    assert plan.profile_uuids == [mine["profile_uuid"]]
    assert plan.cluster_uuids == [mine["cluster_uuid"]]
    assert plan.avatar_paths == [mine["avatar_path"]]


@pytest.fixture
def stores(monkeypatch):
    """Record every external destroy ``purge_account_external_copies`` asks for."""
    from app.services.redis_cache_service import redis_cache

    rec: dict[str, list] = {
        "files": [],
        "speakers": [],
        "profiles": [],
        "clusters": [],
        "avatars": [],
        "caches": [],
    }

    def eraser(kind: str):
        return lambda uuids, fail: rec[kind].extend(uuids)

    def purge_file(plan):
        rec["files"].append(plan)
        return []

    monkeypatch.setattr(account_purge, "_purge_external_copies", purge_file)
    monkeypatch.setattr(account_purge, "_erase_speaker_docs", eraser("speakers"))
    monkeypatch.setattr(account_purge, "_erase_profile_docs", eraser("profiles"))
    monkeypatch.setattr(account_purge, "_erase_cluster_docs", eraser("clusters"))
    monkeypatch.setattr("app.services.minio_service.delete_file", rec["avatars"].append)
    monkeypatch.setattr(redis_cache, "invalidate_all_for_user", rec["caches"].append)
    return rec


def _plan(**overrides) -> account_purge.AccountPurgePlan:
    fields = {
        "files": [{"file_uuid": "f-1"}],
        "avatar_paths": ["avatars/a.png", "avatars/b.png"],
        "speaker_uuids": ["s-1"],
        "profile_uuids": ["p-1"],
        "cluster_uuids": ["c-1"],
        "user_id": 42,
    }
    return account_purge.AccountPurgePlan(**{**fields, **overrides})


def test_the_account_destroy_reaches_every_store(stores):
    residual = account_purge.purge_account_external_copies(_plan())

    assert residual == []
    assert stores["files"] == [{"file_uuid": "f-1"}]
    assert stores["speakers"] == ["s-1"]
    assert stores["profiles"] == ["p-1"]
    assert stores["clusters"] == ["c-1"]
    assert stores["avatars"] == ["avatars/a.png", "avatars/b.png"]
    assert stores["caches"] == [42]


def test_a_failed_avatar_delete_is_reported_and_does_not_stop_the_rest(stores, monkeypatch):
    deleted: list[str] = []

    def delete_file(path: str) -> None:
        if path == "avatars/a.png":
            raise RuntimeError("object storage unreachable")
        deleted.append(path)

    monkeypatch.setattr("app.services.minio_service.delete_file", delete_file)

    residual = account_purge.purge_account_external_copies(_plan())

    assert residual == [
        {"stage": "avatar", "avatar_path": "avatars/a.png", "error": "object storage unreachable"}
    ]
    assert deleted == ["avatars/b.png"]
