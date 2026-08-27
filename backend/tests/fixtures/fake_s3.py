"""Minimal in-memory S3 stand-in for the boto3 client boundary.

Extracted from ``tests/unit/test_backup_service.py`` (issue #600) once a second consumer
(``tests/unit/test_backup_fetch.py``) needed it — a fixture used by exactly one file belongs
in that file; a fixture used by two belongs here, so the two definitions cannot drift.

Two independent stores, on purpose: ``objects`` (key -> size) is all the original consumer
needed (listing/pruning tests never read object bytes back), while ``_contents`` (key ->
bytes) exists only for the download half (``fetch_backup.py``'s ``head_object`` +
``download_file``). Passing ``contents=`` populates both, so a listing-only test can keep
constructing ``_FakeS3Client(objects={...})`` exactly as before.
"""

from __future__ import annotations

from pathlib import Path


class FakeS3Client:
    """Minimal in-memory S3 stand-in for the boto3 client boundary."""

    def __init__(self, *, bucket_ok: bool = True, objects=None, contents=None):
        self._bucket_ok = bucket_ok
        # objects: {key: size}. contents: {key: bytes} — optional, only needed by consumers
        # that read object bodies back (head_object's ContentLength, download_file).
        self.objects: dict[str, int] = dict(objects or {})
        self._contents: dict[str, bytes] = dict(contents or {})
        for key, data in self._contents.items():
            self.objects.setdefault(key, len(data))
        self.uploaded: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def head_bucket(self, Bucket):  # noqa: N803 - boto3 kwarg name
        if not self._bucket_ok:
            raise RuntimeError("bucket not found")
        return {}

    def upload_file(self, local_path, bucket, key):
        data = Path(local_path).read_bytes()
        self.objects[key] = len(data)
        self._contents[key] = data
        self.uploaded.append((bucket, key))

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)
        self._contents.pop(Key, None)
        self.deleted.append(Key)

    def list_objects_v2(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        contents = [{"Key": k, "Size": v} for k, v in self.objects.items() if k.startswith(prefix)]
        return {"Contents": contents}

    def get_paginator(self, _name):
        client = self

        class _Paginator:
            def paginate(self, **kwargs):
                yield client.list_objects_v2(**kwargs)

        return _Paginator()

    def head_object(self, Bucket, Key):  # noqa: N803 - boto3 kwarg name
        if Key not in self.objects:
            raise RuntimeError(f"NoSuchKey: {Key}")
        return {"ContentLength": self.objects[Key]}

    def download_file(self, bucket, key, filename):
        if key not in self._contents:
            raise RuntimeError(
                f"NoSuchKey: {key} (FakeS3Client has no body for it — pass contents=)"
            )
        Path(filename).write_bytes(self._contents[key])
