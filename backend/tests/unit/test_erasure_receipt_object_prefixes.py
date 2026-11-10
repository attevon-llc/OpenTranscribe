"""Which object-storage prefixes an account erasure destroys, and which ones its receipt counts.

An object key names the UPLOADER (``user_<uid>/file_<fid>/...``, ``utils/filename.py``) and never
moves, while an organization content transfer re-owns the ROW. Erasing the uploader must not
sweep ``user_<uid>/`` whole while a file under it belongs to someone else or is held, and must
reach the subject's own files wherever their keys live. The same goes for ``avatars/<uid>/``.

Object storage is an in-memory bucket behind the two seams (``remove_completely`` for the destroy,
``_count_versions`` and the client's listing for the receipt), so these run against the real
Postgres only; the live-store version is ``integration/test_erasure_receipt_e2e.py``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.models.speaker import SpeakerProfile
from app.services.erasure_receipt import destroy_extras
from app.services.erasure_receipt import stores_external
from app.services.erasure_receipt.capture import capture
from app.services.erasure_receipt.types import ErasureScope
from app.utils.filename import get_safe_storage_filename
from tests.fixtures.tag_world import make_file
from tests.fixtures.tag_world import make_org
from tests.fixtures.tag_world import make_user


def _file(db, owner, uploader, org_id=None, *, held=False):
    """A recording owned by ``owner`` whose objects sit under ``uploader``'s prefix."""
    row = make_file(db, owner, org_id)
    row.storage_path = get_safe_storage_filename("Team Sync.mp4", uploader.id, row.id)
    row.thumbnail_path = get_safe_storage_filename("thumbnail.webp", uploader.id, row.id)
    db.commit()
    if held:
        db.execute(text("UPDATE media_file SET legal_hold = true WHERE id = :i"), {"i": row.id})
        db.commit()
    return row


def _profile(db, owner, uploader, org_id=None):
    profile = SpeakerProfile(user_id=owner.id, organization_id=org_id, name=f"p-{owner.id}")
    profile.uuid = uuid_pkg.uuid4()
    profile.avatar_path = f"avatars/{uploader.id}/{profile.uuid}/{uuid_pkg.uuid4().hex}.png"
    db.add(profile)
    db.commit()
    return profile


class FakeBucket:
    """Object keys and their version counts; lists every version, as ``include_version`` does."""

    def __init__(self) -> None:
        self.versions: dict[str, int] = {}
        self.uploads: list[str] = []  # object names of unfinished multipart uploads

    def _list_multipart_uploads(self, bucket, prefix=""):
        return SimpleNamespace(
            uploads=[
                SimpleNamespace(object_name=n, upload_id=f"up-{n}")
                for n in self.uploads
                if n.startswith(prefix)
            ]
        )

    def list_objects(self, bucket, prefix="", recursive=True, include_version=False):
        for name, count in sorted(self.versions.items()):
            if name.startswith(prefix):
                for v in range(count if include_version else 1):
                    yield SimpleNamespace(object_name=name, is_dir=False, version_id=str(v))

    def under(self, prefix: str, *, exact: bool = False) -> int:
        return sum(
            n for k, n in self.versions.items() if (k == prefix if exact else k.startswith(prefix))
        )


@pytest.fixture()
def recorded(monkeypatch):
    bucket = FakeBucket()
    calls: dict = {"removed": [], "removed_keys": set(), "counted": [], "bucket": bucket}

    def remove(client, bucket_name, *, names=None, prefix=None):
        if prefix is not None:
            calls["removed"].append(prefix)
            gone = [k for k in bucket.versions if k.startswith(prefix)]
        else:
            gone = [k for k in names if k in bucket.versions]
            calls["removed_keys"] |= set(names)
        for key in gone:
            del bucket.versions[key]

    def count(bucket_name, prefix, *, exact=False):
        calls["counted"].append(prefix)
        return bucket.under(prefix, exact=exact)

    monkeypatch.setattr("app.services.storage_versions.remove_completely", remove)
    monkeypatch.setattr("app.services.minio_service.minio_client", bucket)
    monkeypatch.setattr(stores_external, "_count_versions", count)
    return calls


def _swept(db, recorded, scope):
    captured = capture(db, scope)
    destroy_extras.erase_object_prefixes(scope, captured, [])
    stores_external._s3_file_objects(scope, captured)
    stores_external._s3_avatars(scope, captured)
    return captured, set(recorded["removed"]), set(recorded["counted"])


def test_uploader_erasure_spares_files_and_photos_handed_to_another_owner(db_session, recorded):
    db = db_session
    org = make_org(db, "xfer")
    a, b, c = make_user(db, "a"), make_user(db, "b"), make_user(db, "c")
    handed = _file(db, a, b, org.id)
    handed_held = _file(db, a, b, org.id, held=True)
    own = _file(db, b, b, org.id)
    received = _file(db, b, c, org.id)
    handed_profile = _profile(db, a, b, org.id)
    own_profile = _profile(db, b, b, org.id)

    captured, removed, counted = _swept(db, recorded, ErasureScope("user", user_id=b.id))

    assert f"user_{b.id}/" not in removed
    assert f"avatars/{b.id}/" not in removed
    for kept in (handed, handed_held):
        assert not any(p.startswith(f"user_{b.id}/file_{kept.id}/") for p in removed), kept.id
    assert removed == {f"user_{b.id}/file_{own.id}/", f"user_{c.id}/file_{received.id}/"}
    # The receipt counts exactly what was destroyed, plus the subject's own photo by key.
    assert counted == removed | {str(own_profile.avatar_path)}
    assert str(handed_profile.avatar_path) not in counted
    assert captured.avatar_paths == [str(own_profile.avatar_path)]


def test_sole_owner_erasure_still_sweeps_the_whole_prefixes(db_session, recorded):
    db = db_session
    b = make_user(db, "solo")
    _file(db, b, b)
    _profile(db, b, b)

    _, removed, counted = _swept(db, recorded, ErasureScope("user", user_id=b.id))

    assert removed == {f"user_{b.id}/", f"avatars/{b.id}/"}
    assert counted == removed, "a photo under the swept prefix is counted once, by the prefix"


def test_a_key_of_another_shape_names_no_prefix(db_session, recorded):
    """A prefix guessed from an unrecognised key could reach another file's objects."""
    db = db_session
    org = make_org(db, "shape")
    b = make_user(db, "sb")
    make_file(db, b, org.id)  # storage_path "tagchar/<uuid>.mp3": not the pipeline's layout

    _, removed, _ = _swept(db, recorded, ErasureScope("org_member", b.id, org.id))

    assert removed == set(), removed


def test_objects_no_surviving_row_names_go_with_a_shared_prefix_and_are_counted(
    db_session, recorded
):
    """``user_<uid>/`` and ``avatars/<uid>/`` cannot go whole while another owner's file or photo
    sits there, but what no surviving row names is still the subject's personal data: a legacy
    ``youtube_<id>/`` thumbnail, an orphaned file directory, a stale photo. All of it goes and the
    receipt counts exactly that; the surviving rows' directories and keys stay untouched."""
    db = db_session
    org = make_org(db, "residue")
    a, b = make_user(db, "ra"), make_user(db, "rb")
    handed = _file(db, a, b, org.id)
    held = _file(db, b, b, org.id, held=True)
    own = _file(db, b, b, org.id)
    legacy = make_file(db, a, org.id)  # handed over too, with a key of a pre-layout shape
    legacy.storage_path = f"user_{b.id}/imports/legacy-clip.mp3"
    db.commit()
    handed_profile = _profile(db, a, b, org.id)
    own_profile = _profile(db, b, b, org.id)

    up, av = f"user_{b.id}/", f"avatars/{b.id}/"
    survivors = {
        str(handed.storage_path): 2,
        str(handed.thumbnail_path): 1,
        f"{up}file_{handed.id}/derived/waveform.json": 1,  # in a surviving dir, named by no row
        str(held.storage_path): 1,
        str(legacy.storage_path): 1,
        str(handed_profile.avatar_path): 1,
        f"{av}{handed_profile.uuid}/previous.png": 1,
        f"user_{b.id}0/file_1/other-account.mp4": 1,  # user_<b>0/ is another account
        f"avatars/{b.id}0/x/other.png": 1,
    }
    own_dir = {str(own.storage_path): 3, str(own.thumbnail_path): 1}
    own_photo = {str(own_profile.avatar_path): 2}
    residue = {
        f"{up}youtube_dQw4w9WgXcQ/thumbnail.jpg": 2,
        f"{up}file_{own.id + 100000}/orphan.wav": 1,
        f"{up}imports/another-legacy.mp3": 1,
        f"{av}{own_profile.uuid}/stale.png": 1,
        f"{av}loose.png": 1,
    }
    bucket = recorded["bucket"]
    bucket.versions = {**survivors, **own_dir, **own_photo, **residue}
    scope = ErasureScope("user", user_id=b.id)

    captured = capture(db, scope)
    before = stores_external._s3_file_objects(scope, captured)
    before += stores_external._s3_avatars(scope, captured)
    errors: list = []
    destroy_extras.erase_object_prefixes(scope, captured, errors)
    for key in captured.avatar_paths:  # ``_erase_avatar_objects`` deletes the erased photos by key
        bucket.versions.pop(key, None)

    assert errors == []
    assert bucket.versions == survivors
    assert recorded["removed_keys"] == set(residue)
    assert before == sum(own_dir.values()) + sum(own_photo.values()) + sum(residue.values())
    after = stores_external._s3_file_objects(scope, captured)
    after += stores_external._s3_avatars(scope, captured)
    assert after == 0


def test_unfinished_uploads_no_surviving_row_names_are_aborted_and_counted(
    db_session, recorded, monkeypatch
):
    db = db_session
    org = make_org(db, "mp")
    a, b = make_user(db, "ma"), make_user(db, "mb")
    handed = _file(db, a, b, org.id)
    own = _file(db, b, b, org.id)
    bucket = recorded["bucket"]
    kept = f"user_{b.id}/file_{handed.id}/re-upload.mp4"
    taken = [f"user_{b.id}/file_{own.id}/part.mp4", f"user_{b.id}/file_{own.id + 100000}/x.mp4"]
    bucket.uploads = [kept, *taken]
    aborted: list[str] = []

    def abort(name: str, upload_id: str) -> bool:
        aborted.append(name)
        bucket.uploads.remove(name)
        return True

    monkeypatch.setattr("app.services.multipart_upload.abort_upload", abort)
    scope = ErasureScope("user", user_id=b.id)
    captured = capture(db, scope)

    assert stores_external._s3_multipart(scope, captured) == len(taken)
    errors: list = []
    destroy_extras.abort_multipart(scope, captured, errors)

    assert errors == []
    assert sorted(aborted) == sorted(taken)
    assert bucket.uploads == [kept]
    assert stores_external._s3_multipart(scope, captured) == 0
