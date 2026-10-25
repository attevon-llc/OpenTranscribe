"""The platform-staff overlay on the capability map (issue #1274).

A resolver may hide a platform-audience surface from tenants. Platform staff
(active ``is_superuser``) must still see what the server lets them use, and that is
exactly the set the ``require_capability`` bypass admits. These pin the rule in core,
independent of any resolver, because the community resolver returns all-true and
could never exercise it.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from fastapi import Request

from app.core import capabilities
from app.core.capabilities import AUDIENCE_PLATFORM
from app.core.capabilities import CAPABILITY_AUDIENCE
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import NO_PLATFORM_BYPASS
from app.core.capabilities import PLATFORM_OVERLAY_KEYS
from app.core.capabilities import get_capabilities
from app.core.capabilities import get_capabilities_for_viewer
from app.core.capabilities import is_platform_staff
from app.core.capabilities import require_capability
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver

ALL_DENIED = dict.fromkeys(COMMUNITY_CAPABILITIES, False)


def _user(*, superuser: bool, active: bool = True, role: str | None = None) -> Any:
    return SimpleNamespace(
        is_superuser=superuser,
        is_active=active,
        role=role or ("super_admin" if superuser else "user"),
    )


@pytest.fixture(autouse=True)
def _community_resolver(monkeypatch) -> Iterator[None]:
    monkeypatch.setattr(capabilities, "_warned", set(), raising=False)
    reset_capability_resolver()
    yield
    reset_capability_resolver()


def _deny_everything() -> None:
    set_capability_resolver(lambda _req: dict(ALL_DENIED))


# -- the declared sets ------------------------------------------------------------


def test_platform_overlay_keys_are_platform_audience_minus_no_bypass():
    expected = {k for k, a in CAPABILITY_AUDIENCE.items() if a == AUDIENCE_PLATFORM}
    expected -= NO_PLATFORM_BYPASS

    assert frozenset(expected) == PLATFORM_OVERLAY_KEYS
    # A non-empty set: an empty overlay would make every viewer test below vacuous.
    assert "admin.takedown" in PLATFORM_OVERLAY_KEYS
    assert "audit.logs" in PLATFORM_OVERLAY_KEYS


def test_no_bypass_keys_are_known_capabilities():
    """A typo in the declared set would silently let the overlay flip a locked key."""
    assert frozenset(COMMUNITY_CAPABILITIES) >= NO_PLATFORM_BYPASS
    assert (
        frozenset(
            {
                "org.takedown",
                "org.audit_logs",
                "org.legal_hold",
                "speaker_attributes.migration",
                "url_ingest",
                "audio_extraction",
                "media_sources",
                "admin.flower",
                "transcription.model_choice",
                "transcription.diarization_source",
                "transcription.advanced",
            }
        )
        == NO_PLATFORM_BYPASS
    )
    assert not (PLATFORM_OVERLAY_KEYS & NO_PLATFORM_BYPASS)


def test_require_capability_rejects_undeclared_no_bypass_key():
    """Declaring a no-bypass gate is how the overlay learns not to override it."""
    with pytest.raises(ValueError, match="NO_PLATFORM_BYPASS"):
        require_capability("chat.rag", platform_admin_bypass=False)


def test_require_capability_accepts_declared_no_bypass_key():
    assert callable(require_capability("url_ingest", platform_admin_bypass=False))


# -- who is staff -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (_user(superuser=True), True),
        (_user(superuser=True, active=False), False),
        (_user(superuser=False, role="admin"), False),
        (_user(superuser=False), False),
        (None, False),
    ],
    ids=["super_admin", "inactive_super_admin", "admin", "user", "anonymous"],
)
def test_is_platform_staff(user, expected):
    assert is_platform_staff(user) is expected


# -- the overlay ------------------------------------------------------------------


def test_overlay_denied_to_user_admin_and_anonymous():
    """Tenants and anonymous callers get the resolver's answer, byte for byte."""
    _deny_everything()
    request = cast(Request, SimpleNamespace(state=SimpleNamespace()))

    for viewer in (None, _user(superuser=False), _user(superuser=False, role="admin")):
        assert get_capabilities_for_viewer(request, viewer) == ALL_DENIED


def test_overlay_denied_to_inactive_super_admin():
    _deny_everything()

    assert get_capabilities_for_viewer(None, _user(superuser=True, active=False)) == ALL_DENIED


def test_platform_key_denied_by_resolver_is_shown_to_staff():
    set_capability_resolver(lambda _req: {**COMMUNITY_CAPABILITIES, "admin.takedown": False})

    assert get_capabilities_for_viewer(None, _user(superuser=False))["admin.takedown"] is False
    assert get_capabilities_for_viewer(None, _user(superuser=True))["admin.takedown"] is True


def test_overlay_grants_exactly_the_overlay_keys_when_everything_is_denied():
    _deny_everything()

    caps = get_capabilities_for_viewer(None, _user(superuser=True))

    assert {k for k, v in caps.items() if v} == set(PLATFORM_OVERLAY_KEYS)


def test_no_bypass_keys_are_never_overlaid():
    """The deployment owns these: the server refuses staff too, so the UI must not offer them."""
    _deny_everything()

    caps = get_capabilities_for_viewer(None, _user(superuser=True))

    assert NO_PLATFORM_BYPASS
    for key in NO_PLATFORM_BYPASS:
        assert caps[key] is False, key


def test_non_platform_keys_are_never_overlaid():
    """User, team and org-admin keys describe the tenant's product, not staff tools."""
    _deny_everything()

    caps = get_capabilities_for_viewer(None, _user(superuser=True))

    tenant_keys = [k for k, a in CAPABILITY_AUDIENCE.items() if a != AUDIENCE_PLATFORM]
    assert tenant_keys
    for key in tenant_keys:
        assert caps[key] is False, key


def test_overlay_applies_when_resolver_raises():
    def _boom(_req):
        raise RuntimeError("resolver down")

    set_capability_resolver(_boom)

    staff = get_capabilities_for_viewer(None, _user(superuser=True))
    tenant = get_capabilities_for_viewer(None, _user(superuser=False))

    assert {k for k, v in staff.items() if v} == set(PLATFORM_OVERLAY_KEYS)
    assert tenant == ALL_DENIED


def test_overlay_applies_when_resolver_returns_a_non_dict():
    set_capability_resolver(lambda _req: cast(Any, None))

    staff = get_capabilities_for_viewer(None, _user(superuser=True))

    assert {k for k, v in staff.items() if v} == set(PLATFORM_OVERLAY_KEYS)


def test_viewer_map_does_not_mutate_the_resolver_result():
    """The overlay builds a new dict, so one viewer's map can never leak to the next."""
    shared = dict(ALL_DENIED)
    set_capability_resolver(lambda _req: shared)

    get_capabilities_for_viewer(None, _user(superuser=True))

    assert shared == ALL_DENIED
    assert get_capabilities() == ALL_DENIED


def test_community_map_is_unchanged_for_every_viewer():
    """Community resolver is all-true for platform keys already: nothing to overlay."""
    for viewer in (None, _user(superuser=False), _user(superuser=True)):
        assert get_capabilities_for_viewer(None, viewer) == COMMUNITY_CAPABILITIES
