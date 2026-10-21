"""The authority table of issue #1253, one test per cell, with no database.

``takedown_authority`` decides who may take a file down or release it. The rows below
are the whole table from its module docstring; a row that is not asserted is a row whose
removal nobody would notice. Rows that REFUSE also assert the verdict's HTTP status and
code, because the routes translate exactly those two values.
"""

from __future__ import annotations

import pytest

from app.core.enums import TakedownOrigin
from app.models.media import MediaFile
from app.models.user import User
from app.services.takedown_authority import CODE_NOT_FOUND
from app.services.takedown_authority import CODE_PLATFORM_ORIGIN
from app.services.takedown_authority import TakedownActor
from app.services.takedown_authority import TakedownRefusedError
from app.services.takedown_authority import assert_may_quarantine
from app.services.takedown_authority import assert_may_release
from app.services.takedown_authority import current_origin
from app.services.takedown_authority import origin_after_release

ORG = 7
OTHER_ORG = 8


def _file(*, quarantined=False, held=False, origin=None, org=ORG) -> MediaFile:
    return MediaFile(
        is_quarantined=quarantined,
        legal_hold=held,
        takedown_origin=origin,
        organization_id=org,
    )


def _platform() -> TakedownActor:
    return TakedownActor.platform(User(id=1))


def _org(org: int = ORG) -> TakedownActor:
    return TakedownActor.organization(User(id=2), org)


class TestCurrentOrigin:
    def test_a_file_nobody_holds_has_no_origin(self):
        assert current_origin(_file()) is None

    @pytest.mark.parametrize("flags", [{"quarantined": True}, {"held": True}])
    def test_a_held_file_reports_its_recorded_origin(self, flags):
        assert current_origin(_file(origin="organization", **flags)) is TakedownOrigin.ORGANIZATION
        assert current_origin(_file(origin="platform", **flags)) is TakedownOrigin.PLATFORM

    @pytest.mark.parametrize("origin", [None, "", "tenant", "ORGANIZATION"])
    def test_a_held_file_with_no_usable_origin_reads_as_platform(self, origin):
        """Fail closed: the error that must be impossible is an organization able to
        release something the operator placed."""
        assert current_origin(_file(quarantined=True, origin=origin)) is TakedownOrigin.PLATFORM

    def test_a_stale_origin_on_an_unheld_file_is_ignored(self):
        assert current_origin(_file(origin="platform")) is None


class TestQuarantine:
    def test_platform_may_take_down_a_clean_file(self):
        assert_may_quarantine(_platform(), _file())

    def test_platform_may_take_down_an_organization_held_file(self):
        assert_may_quarantine(_platform(), _file(quarantined=True, origin="organization"))

    def test_platform_may_refresh_its_own_takedown(self):
        assert_may_quarantine(_platform(), _file(quarantined=True, origin="platform"))

    def test_organization_may_take_down_a_clean_file_of_its_own(self):
        assert_may_quarantine(_org(), _file())

    def test_organization_may_refresh_its_own_takedown(self):
        assert_may_quarantine(_org(), _file(quarantined=True, origin="organization"))

    def test_organization_is_refused_on_a_platform_takedown(self):
        with pytest.raises(TakedownRefusedError) as refused:
            assert_may_quarantine(_org(), _file(quarantined=True, origin="platform"))
        assert (refused.value.status_code, refused.value.code) == (409, CODE_PLATFORM_ORIGIN)

    def test_organization_is_refused_on_a_released_but_still_platform_held_file(self):
        with pytest.raises(TakedownRefusedError) as refused:
            assert_may_quarantine(_org(), _file(held=True, origin="platform"))
        assert refused.value.code == CODE_PLATFORM_ORIGIN

    def test_organization_is_refused_on_a_flagged_file_with_no_origin(self):
        with pytest.raises(TakedownRefusedError) as refused:
            assert_may_quarantine(_org(), _file(quarantined=True, origin=None))
        assert refused.value.code == CODE_PLATFORM_ORIGIN

    def test_organization_is_refused_on_another_organizations_file(self):
        with pytest.raises(TakedownRefusedError) as refused:
            assert_may_quarantine(_org(), _file(org=OTHER_ORG))
        assert (refused.value.status_code, refused.value.code) == (404, CODE_NOT_FOUND)

    def test_organization_is_refused_on_a_personal_file(self):
        with pytest.raises(TakedownRefusedError) as refused:
            assert_may_quarantine(_org(), _file(org=None))
        assert refused.value.status_code == 404


class TestRelease:
    def test_platform_may_release_a_platform_takedown(self):
        assert_may_release(_platform(), _file(quarantined=True, origin="platform"))

    def test_platform_may_release_an_organization_takedown(self):
        assert_may_release(_platform(), _file(quarantined=True, origin="organization"))

    def test_organization_may_release_its_own_takedown(self):
        assert_may_release(_org(), _file(quarantined=True, origin="organization"))

    def test_organization_is_refused_on_a_platform_takedown(self):
        with pytest.raises(TakedownRefusedError) as refused:
            assert_may_release(_org(), _file(quarantined=True, origin="platform"))
        assert (refused.value.status_code, refused.value.code) == (403, CODE_PLATFORM_ORIGIN)

    def test_organization_is_refused_on_a_flagged_file_with_no_origin(self):
        with pytest.raises(TakedownRefusedError):
            assert_may_release(_org(), _file(held=True, origin=None))

    def test_organization_is_refused_on_another_organizations_file(self):
        with pytest.raises(TakedownRefusedError) as refused:
            assert_may_release(
                _org(), _file(quarantined=True, origin="organization", org=OTHER_ORG)
            )
        assert refused.value.status_code == 404


class TestOriginAfterRelease:
    def test_nothing_left_holding_the_file_clears_the_origin(self):
        assert origin_after_release(_file(origin="organization")) is None

    def test_a_release_that_keeps_the_hold_keeps_the_holder(self):
        assert origin_after_release(_file(held=True, origin="platform")) is TakedownOrigin.PLATFORM
        assert (
            origin_after_release(_file(held=True, origin="organization"))
            is TakedownOrigin.ORGANIZATION
        )


class TestActor:
    def test_authority_follows_the_constructor_not_the_users_roles(self):
        """A person who is both a platform admin and an org admin acts as whichever
        route built the actor; the user object cannot widen it."""
        both = User(id=3, role="super_admin", is_superuser=True)
        acting_for_org = TakedownActor.organization(both, ORG)
        assert acting_for_org.authority is TakedownOrigin.ORGANIZATION
        assert acting_for_org.role_label == "org_admin"
        with pytest.raises(TakedownRefusedError):
            assert_may_release(acting_for_org, _file(quarantined=True, origin="platform"))
        assert TakedownActor.platform(both).role_label == "platform_admin"
