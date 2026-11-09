"""``app/utils/db_helpers.py`` — the tenant-scope choke point and query builders.

These are exercised against the real Postgres savepoint (``db_session``) rather
than a mocked ``Session``/``Query``, so a wrong filter clause actually fails the
assertion instead of a mock silently accepting whatever was called on it.
"""

from __future__ import annotations

import uuid

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.organization import Organization


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _make_org(db_session) -> Organization:
    org = Organization(name=f"org-{_suffix()}")
    db_session.add(org)
    db_session.flush()
    return org


def _make_file(db_session, owner, *, status=FileStatus.COMPLETED, **kwargs) -> MediaFile:
    file_uuid = uuid.uuid4()
    defaults = dict(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"dbhelpers-{file_uuid.hex[:8]}.wav",
        storage_path=f"dbhelpers-test/{file_uuid.hex}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=status,
    )
    defaults.update(kwargs)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.flush()
    return media_file


# ---------------------------------------------------------------------------
# get_user_files_query
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# safe_get_by_id
# ---------------------------------------------------------------------------
