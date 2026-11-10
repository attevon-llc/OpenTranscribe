"""Which object-storage prefixes an account erasure destroys, and which ones its receipt counts.

An object key names the UPLOADER (``user_<uid>/file_<fid>/...``, ``utils/filename.py``) and never
moves, while an organization content transfer re-owns the ROW. Erasing the uploader must not
sweep ``user_<uid>/`` whole while a file under it belongs to someone else or is held, and must
reach the subject's own files wherever their keys live. The same goes for ``avatars/<uid>/``.

Object storage is replaced by recorders at the two seams (``remove_completely`` for the destroy,
``_count_versions`` for the receipt), so these run against the real Postgres only; the live-store
version is ``integration/test_erasure_receipt_e2e.py``.
"""

from __future__ import annotations

import uuid as uuid_pkg

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


@pytest.fixture()
def recorded(monkeypatch):
    calls: dict[str, list[str]] = {"removed": [], "counted": []}

    def remove(client, bucket, *, names=None, prefix=None):
        calls["removed"].append(prefix)

    def count(bucket, prefix, *, exact=False):
        calls["counted"].append(prefix)
        return 0

    monkeypatch.setattr("app.services.storage_versions.remove_completely", remove)
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
