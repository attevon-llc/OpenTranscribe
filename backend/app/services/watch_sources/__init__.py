"""Watch Sources: auto-import media from local folders, S3 buckets, and SMB shares.

Public surface:
  - ``create_client(source)`` — build the right backend client for a source.
  - ``import_single_file(...)`` — run one discovered file through the ingest pipeline.
  - ``folder_browser`` — back the local folder picker.
  - ``multipart`` — detect/stitch split recordings.
"""

from app.services.watch_sources.base import BaseWatchSourceClient
from app.services.watch_sources.base import RemoteFileInfo
from app.services.watch_sources.base import create_client
from app.services.watch_sources.base import parse_extensions
from app.services.watch_sources.processing import import_single_file
from app.services.watch_sources.processing import ingest_prepared_file

__all__ = [
    "BaseWatchSourceClient",
    "RemoteFileInfo",
    "create_client",
    "parse_extensions",
    "import_single_file",
    "ingest_prepared_file",
]
