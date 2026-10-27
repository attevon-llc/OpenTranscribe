"""Create the media bucket by hand, using the application's own storage configuration.

Backend startup already does this (``app.main`` creates ``MEDIA_BUCKET_NAME`` when absent), so
this is only for provisioning storage before the API has ever started. It deliberately goes
through ``app.services.minio_service.ensure_bucket_exists`` instead of building its own client:
the earlier standalone version read ``MINIO_SERVER`` (no other file uses it; the app reads
``MINIO_HOST``), defaulted to port 9090, and set a public-read policy that no deployment of this
app wants -- media is served through presigned URLs, and a world-readable bucket bypasses
quarantine.

Run inside the backend container: ``./opentr.sh shell backend`` then
``python scripts/create_minio_bucket.py``.
"""

from app.core.config import settings
from app.services.minio_service import ensure_bucket_exists


def main() -> None:
    ensure_bucket_exists()
    print(f"Bucket '{settings.MEDIA_BUCKET_NAME}' exists")


if __name__ == "__main__":
    main()
