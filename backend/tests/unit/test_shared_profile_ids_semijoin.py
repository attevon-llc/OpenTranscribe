"""Shared speaker-profile ids come from a semi-join, not a DISTINCT over a 4-table join (#1337, D2)."""

from __future__ import annotations

import pytest

from app.core.tenancy import UNSCOPED
from app.services.permission_service import PermissionService
from tests.query_budget import count_statements
from tests.unit.test_speaker_profiles_list_query_budget import _shared_profile

pytestmark = pytest.mark.unit


def test_shared_profiles_are_found_without_distinct_or_media_file_join(db_session, normal_user):
    _shared_profile(db_session, normal_user, 3)
    with count_statements(db_session.connection()) as log:
        ids = PermissionService.get_accessible_profile_ids(
            db_session, normal_user.id, organization_id=UNSCOPED
        )
    assert len(ids) == 3  # control: every shared profile is still reachable
    shared = log.matching(r"collection_share")
    assert shared, log.describe()
    assert not [s for s in shared if "DISTINCT" in s or "JOIN media_file" in s], shared
    with_source = PermissionService.get_accessible_profile_ids_with_source(
        db_session, normal_user.id, organization_id=UNSCOPED
    )
    assert {pid for pid, owned in with_source if not owned} == ids
