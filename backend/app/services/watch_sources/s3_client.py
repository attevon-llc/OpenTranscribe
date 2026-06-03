"""S3-compatible watch-source client (boto3).

Works against AWS S3, MinIO, Backblaze B2, Wasabi, etc. via an explicit
``endpoint_url``. Lists objects under a prefix (paginated, handles >1000),
copies them to a local temp path for import, and can write stitched output back.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import PurePosixPath

from app.services.watch_sources.base import BaseWatchSourceClient
from app.services.watch_sources.base import RemoteFileInfo

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 30


class S3WatchClient(BaseWatchSourceClient):
    """Watch an S3-compatible bucket/prefix."""

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        bucket: str | None,
        prefix: str = "",
        region: str | None = None,
        access_key_id: str | None = None,
        secret_key: str | None = None,
        use_ssl: bool = True,
    ) -> None:
        if not bucket:
            raise ValueError("S3 source requires a bucket name")
        self._endpoint_url = endpoint_url or None
        self._bucket = bucket
        self._prefix = (prefix or "").lstrip("/")
        self._region = region or None
        self._access_key_id = access_key_id or None
        self._secret_key = secret_key or None
        self._use_ssl = use_ssl
        self._cached_client = None

    def _client(self):
        if self._cached_client is None:
            import boto3
            from botocore.config import Config

            self._cached_client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
                use_ssl=self._use_ssl,
                config=Config(
                    connect_timeout=_CONNECT_TIMEOUT,
                    read_timeout=_READ_TIMEOUT,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        return self._cached_client

    def test_connection(self) -> tuple[bool, str]:
        try:
            self._client().head_bucket(Bucket=self._bucket)
            return True, f"OK — bucket '{self._bucket}' reachable"
        except Exception as e:  # noqa: BLE001
            return False, f"S3 connection failed: {e}"

    def list_files(
        self,
        extensions: list[str] | None = None,
        recursive: bool = True,
        min_modified: datetime | None = None,
    ) -> list[RemoteFileInfo]:
        client = self._client()
        paginator = client.get_paginator("list_objects_v2")
        results: list[RemoteFileInfo] = []

        # When not recursive, restrict to the immediate prefix level via Delimiter.
        kwargs = {"Bucket": self._bucket, "Prefix": self._prefix}
        if not recursive:
            kwargs["Delimiter"] = "/"

        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):  # pseudo-directory marker
                    continue
                name = PurePosixPath(key).name
                if extensions and PurePosixPath(name).suffix.lower() not in extensions:
                    continue
                modified = obj.get("LastModified")
                if min_modified and modified and modified < min_modified:
                    continue
                results.append(
                    RemoteFileInfo(
                        path=key,
                        name=name,
                        size=int(obj.get("Size", 0)),
                        modified_time=modified,
                    )
                )
        return results

    def download_file(self, remote_path: str, local_path: str) -> int:
        client = self._client()
        # Resume: skip download if a correctly-sized temp already exists.
        expected = None
        try:
            head = client.head_object(Bucket=self._bucket, Key=remote_path)
            expected = int(head.get("ContentLength", 0))
        except Exception as e:  # noqa: BLE001
            logger.debug("head_object failed for %s: %s", remote_path, e)
        if expected and os.path.exists(local_path) and os.path.getsize(local_path) == expected:
            return expected
        client.download_file(self._bucket, remote_path, local_path)
        size = os.path.getsize(local_path)
        if expected and size != expected:
            raise RuntimeError(f"S3 download size mismatch ({size} != {expected})")
        return size

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        self._client().upload_file(local_path, self._bucket, remote_path.lstrip("/"))
        return True

    def close(self) -> None:
        self._cached_client = None
