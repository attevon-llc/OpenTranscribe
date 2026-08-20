"""The org tenant gate on the chat aggregation tier's Postgres shapes (W2.0g fix #2).

`_files_in_period` and `_occurrence_count` each called
``PermissionService.get_accessible_file_ids_subquery(db, user_id)`` with no
``organization_id`` argument. That default is the ``UNSCOPED`` sentinel — no
tenant gate at all, not "personal scope" — so a *personal*-scope aggregation
(``organization_id=None``) counted a caller's own org-stamped files as if they
were org-less. Both shapes now go through ``_accessible_scoped_files``, which
threads ``organization_id`` through explicitly; these tests reproduce the leak
against a real Postgres organization row and prove the fixed shapes exclude it.

A single user owning both a personal and an org-stamped recording is enough to
show the defect: ownership alone used to be sufficient to count a file
regardless of which tenant it was stamped into, once the gate was dropped.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

from app.services.chat.aggregation_service import _files_in_period
from app.services.chat.aggregation_service import _occurrence_count

pytestmark = pytest.mark.unit

MARCH = ("2025-03-01", "2025-04-01")


@pytest.fixture
def org(db_session):
    from app.models.organization import Organization

    organization = Organization(
        uuid=uuid_pkg.uuid4(),
        external_org_id=f"org-gate-{uuid_pkg.uuid4().hex[:8]}",
        name="Gate Org",
        is_active=True,
    )
    db_session.add(organization)
    db_session.commit()
    db_session.refresh(organization)
    return organization


def _add_file(db, user, *, organization_id, recorded=None, title="clip"):
    from app.models.media import MediaFile

    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        organization_id=organization_id,
        filename=f"{title}.wav",
        title=title,
        storage_path=f"x/{uuid_pkg.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
        recorded_date=recorded,
        recorded_date_source="filename" if recorded is not None else None,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _add_segment(db, media_file, text):
    from app.models.media import TranscriptSegment

    segment = TranscriptSegment(
        media_file_id=media_file.id, start_time=0.0, end_time=5.0, text=text
    )
    db.add(segment)
    db.commit()
    return segment


# ---------------------------------------------------------------------------
# `_occurrence_count` (aggregation_service.py ~line 312)
# ---------------------------------------------------------------------------


def test_occurrence_count_personal_scope_excludes_the_callers_own_org_stamped_file(
    db_session, normal_user, org
):
    """LEAK: an org-stamped file must not count toward a PERSONAL-scope answer,
    even though the same user owns it.

    Before the fix, ``_occurrence_count`` called the accessible-files subquery
    with no ``organization_id`` at all (the ``UNSCOPED`` sentinel), so the
    owned-files branch applied no tenant filter and both recordings counted —
    3 occurrences instead of 1.
    """
    personal = _add_file(db_session, normal_user, organization_id=None, title="Personal")
    _add_segment(db_session, personal, "we discussed the gizmo rollout")

    org_file = _add_file(db_session, normal_user, organization_id=org.id, title="OrgOnly")
    _add_segment(db_session, org_file, "gizmo gizmo gizmo — three mentions here")

    count = _occurrence_count(db_session, "gizmo", normal_user.id, None, None)

    assert count == 1, f"personal-scope count leaked the org-stamped file's occurrences: {count}"


def test_occurrence_count_org_scope_still_counts_its_own_files(db_session, normal_user, org):
    """SHARED-VISIBILITY control: threading the gate through must not also make
    ORG-scoped aggregation blind to the org's own files."""
    org_file = _add_file(db_session, normal_user, organization_id=org.id, title="OrgOnly")
    _add_segment(db_session, org_file, "gizmo gizmo")

    count = _occurrence_count(db_session, "gizmo", normal_user.id, org.id, None)

    assert count == 2


# ---------------------------------------------------------------------------
# `_files_in_period` (aggregation_service.py ~line 149)
# ---------------------------------------------------------------------------


def test_files_in_period_personal_scope_excludes_the_callers_own_org_stamped_file(
    db_session, normal_user, org
):
    """LEAK: same shape, the date-filtered aggregation's Postgres statement."""
    personal = _add_file(
        db_session,
        normal_user,
        organization_id=None,
        recorded=dt.datetime(2025, 3, 10, tzinfo=dt.UTC),
        title="Personal",
    )
    _add_file(
        db_session,
        normal_user,
        organization_id=org.id,
        recorded=dt.datetime(2025, 3, 12, tzinfo=dt.UTC),
        title="OrgOnly",
    )

    found = _files_in_period(db_session, MARCH, normal_user.id, None, None)

    assert found is not None
    uuids, _sources, _undated = found
    assert uuids == [str(personal.uuid)]


def test_files_in_period_org_scope_still_finds_its_own_files(db_session, normal_user, org):
    """SHARED-VISIBILITY control: org-scoped date filtering is unaffected."""
    org_file = _add_file(
        db_session,
        normal_user,
        organization_id=org.id,
        recorded=dt.datetime(2025, 3, 10, tzinfo=dt.UTC),
        title="OrgOnly",
    )

    found = _files_in_period(db_session, MARCH, normal_user.id, org.id, None)

    assert found is not None
    uuids, _sources, _undated = found
    assert uuids == [str(org_file.uuid)]
