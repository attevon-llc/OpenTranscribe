"""``storage_versions.remove_completely`` under refused permissions and partial failure.

A real bucket cannot be made to refuse ``GetBucketVersioning`` or ``ListBucketVersions`` to the
root identity, so this half drives the decision table against an in-memory S3 that does exactly
what the server does for each call. The real versioned-bucket behaviour is
``integration/test_versioned_bucket_erasure.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from minio.error import S3Error
from urllib3.response import HTTPResponse

from app.services import storage_versions as sv


def _denied(op: str, code: str = "AccessDenied") -> S3Error:
    return S3Error(HTTPResponse(status=403), code, f"{op} denied", "res", "req", "host")


class FakeS3:
    """Objects as ``{key: [version ids]}``; each refusal is opt-in."""

    def __init__(
        self,
        *,
        versioned: bool,
        deny_get=False,
        deny_list_versions=False,
        locked=(),
        get_code="AccessDenied",
        missing_bucket=False,
    ):
        self.versioned = versioned
        self.get_code = get_code
        self.missing_bucket = missing_bucket
        self.deny_get = deny_get
        self.deny_list_versions = deny_list_versions
        self.locked = set(locked)
        self.objects: dict[str, list[str]] = {}
        self.versioning_reads = 0

    def put(self, key: str, versions: int = 1) -> None:
        for _ in range(versions if self.versioned else 1):
            vid = f"v{len(self.objects.get(key, [])) + 1}" if self.versioned else "null"
            self.objects.setdefault(key, []).append(vid)

    def get_bucket_versioning(self, bucket):
        self.versioning_reads += 1
        if self.missing_bucket:
            raise S3Error(HTTPResponse(status=404), "NoSuchBucket", "gone", "res", "req", "host")
        if self.deny_get:
            raise _denied("GetBucketVersioning", self.get_code)
        return SimpleNamespace(status="Enabled" if self.versioned else None)

    def list_objects(self, bucket, prefix=None, recursive=False, include_version=False):
        if include_version and self.deny_list_versions:
            raise _denied("ListBucketVersions")
        for key, versions in sorted(self.objects.items()):
            if prefix and not key.startswith(prefix):
                continue
            if include_version:
                for vid in versions:
                    yield SimpleNamespace(object_name=key, version_id=vid)
            elif versions:
                yield SimpleNamespace(object_name=key, version_id=None)

    def remove_objects(self, bucket, targets):
        for target in targets:
            name, vid = target.name, target.version_id
            if (name, vid) in self.locked:
                yield SimpleNamespace(name=name, version_id=vid, code="AccessDenied")
                continue
            versions = self.objects.get(name, [])
            if vid is None and self.versioned:
                versions.append("marker")  # a plain delete on a versioned bucket
            elif vid is None or vid == "null":
                self.objects.pop(name, None)
            elif vid in versions:
                versions.remove(vid)
                if not versions:
                    self.objects.pop(name, None)


@pytest.fixture(autouse=True)
def _fresh_cache():
    sv.forget_versioning_state()
    yield
    sv.forget_versioning_state()


def test_an_unversioned_bucket_deletes_without_listing_versions():
    s3 = FakeS3(versioned=False, deny_list_versions=True)
    s3.put("a")
    assert sv.remove_completely(s3, "b", names=["a"]) == 1
    assert s3.objects == {}


def test_a_versioned_bucket_loses_every_version():
    s3 = FakeS3(versioned=True)
    s3.put("a", versions=3)
    assert sv.remove_completely(s3, "b", names=["a"]) == 3
    assert s3.objects == {}


def test_an_unreadable_versioning_state_still_takes_the_version_path():
    s3 = FakeS3(versioned=True, deny_get=True)
    s3.put("a", versions=2)
    assert sv.remove_completely(s3, "b", names=["a"]) == 2
    assert s3.objects == {}


def test_both_reads_refused_is_unverified_with_an_error_log(caplog):
    """Cannot know whether versions remain: never a failure, never silent, never verified."""
    s3 = FakeS3(versioned=False, deny_get=True, deny_list_versions=True)
    s3.put("a")
    result = sv.remove_completely(s3, "b", names=["a"])
    assert result == 1
    assert result.unverified is True
    assert s3.objects == {}
    assert "s3:ListBucketVersions" in caplog.text


def test_a_known_versioned_bucket_with_listing_refused_reports_failure():
    s3 = FakeS3(versioned=True, deny_list_versions=True)
    s3.put("a", versions=2)
    with pytest.raises(sv.StorageDeleteIncompleteError) as raised:
        sv.remove_completely(s3, "b", names=["a"])
    assert raised.value.remaining == ["a"]


def test_a_locked_version_fails_the_delete_and_is_named():
    s3 = FakeS3(versioned=True, locked={("a", "v1")})
    s3.put("a", versions=2)
    with pytest.raises(sv.StorageDeleteIncompleteError) as raised:
        sv.remove_completely(s3, "b", names=["a"])
    assert any("a@v1" in entry for entry in raised.value.remaining)
    assert s3.objects == {"a": ["v1"]}, "the unlocked version is still removed"


def test_a_verified_delete_is_not_flagged_unverified():
    for versioned in (False, True):
        s3 = FakeS3(versioned=versioned)
        s3.put("a")
        assert sv.remove_completely(s3, "b", names=["a"]).unverified is False


def test_an_off_answer_is_never_cached_so_enabling_versioning_is_seen():
    s3 = FakeS3(versioned=False)
    s3.put("x")
    sv.remove_completely(s3, "b", names=["x"])
    s3.versioned = True
    s3.put("y", versions=2)
    assert sv.remove_completely(s3, "b", names=["y"]) == 2
    assert s3.objects == {}, "versions written after versioning was enabled are removed too"


def test_a_store_without_versioning_is_not_a_denial(caplog):
    s3 = FakeS3(versioned=False, deny_get=True, get_code="NotImplemented", deny_list_versions=True)
    s3.put("a")
    result = sv.remove_completely(s3, "b", names=["a"])
    assert result == 1 and result.unverified is False
    assert "cannot verify" not in caplog.text
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_a_missing_bucket_has_nothing_to_delete():
    result = sv.remove_completely(FakeS3(versioned=False, missing_bucket=True), "b", prefix="d/")
    assert result == 0 and result.unverified is False


def test_an_empty_prefix_is_rejected_like_none():
    s3 = FakeS3(versioned=True)
    s3.put("keep")
    with pytest.raises(ValueError):
        sv.remove_completely(s3, "b", prefix="")
    assert list(s3.objects) == ["keep"]


def test_the_state_is_cached_and_dropped_on_failure():
    s3 = FakeS3(versioned=True, locked={("a", "v1")})
    s3.put("x")
    sv.remove_completely(s3, "b", names=["x"])
    sv.remove_completely(s3, "b", names=["x"])
    assert s3.versioning_reads == 1, "one versioning read per bucket per cache window"
    s3.put("a")
    with pytest.raises(sv.StorageDeleteIncompleteError):
        sv.remove_completely(s3, "b", names=["a"])
    sv.remove_completely(s3, "b", names=["x"])
    assert s3.versioning_reads == 2, "a failed delete drops the cached state"


def test_a_prefix_purge_leaves_keys_outside_the_prefix():
    s3 = FakeS3(versioned=True)
    s3.put("derived/1", versions=2)
    s3.put("keep/1")
    assert sv.remove_completely(s3, "b", prefix="derived/") == 2
    assert list(s3.objects) == ["keep/1"]


def test_exactly_one_selector_is_required():
    with pytest.raises(ValueError):
        sv.remove_completely(FakeS3(versioned=False), "b")
    with pytest.raises(ValueError):
        sv.remove_completely(FakeS3(versioned=False), "b", names=["a"], prefix="a")
