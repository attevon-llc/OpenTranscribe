"""Who may quarantine or release a file: the authority model of issue #1253.

``media_file.takedown_origin`` records which authority holds a file. The rule is
"platform wins":

======================  ==============================  ==================================
Action                  Platform actor                  Organization actor
======================  ==============================  ==================================
quarantine, clean file  allowed, origin = platform      allowed, origin = organization
quarantine, org-held    allowed, escalates to platform  allowed (refreshes the reason)
quarantine, platform    allowed (refreshes the reason)  REFUSED (409, no mutation)
release, org-held       allowed                         allowed
release, platform-held  allowed                         REFUSED (403, no mutation)
======================  ==============================  ==================================

Authority comes from the ROUTE that built the :class:`TakedownActor`, never from the
caller's roles: a person who is both a platform administrator and an organization
administrator and calls an organization route acts as the organization, so they cannot
use that route to touch the operator's takedown.

A flagged row with no recorded origin is read as ``platform`` (fail closed); the
database also refuses to store that state (see migration ``v442``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.enums import TakedownOrigin

if TYPE_CHECKING:
    from app.models.media import MediaFile
    from app.models.user import User

CODE_PLATFORM_ORIGIN = "takedown_platform_origin"
CODE_NOT_FOUND = "takedown_file_not_found"

_PLATFORM_ROLE = "platform_admin"
_ORG_ROLE = "org_admin"


class TakedownRefusedError(Exception):
    """The actor's authority does not cover this action. Nothing was changed."""

    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class TakedownActor:
    """The user performing a takedown action, and the authority they act with."""

    user: User
    authority: TakedownOrigin
    organization_id: int | None = None

    @classmethod
    def platform(cls, user: User) -> TakedownActor:
        """An operator acting through the platform ``/admin`` routes."""
        return cls(user=user, authority=TakedownOrigin.PLATFORM)

    @classmethod
    def organization(cls, user: User, organization_id: int) -> TakedownActor:
        """An organization administrator acting for ``organization_id`` only."""
        return cls(
            user=user, authority=TakedownOrigin.ORGANIZATION, organization_id=int(organization_id)
        )

    @property
    def role_label(self) -> str:
        """The value recorded as ``actor_role`` in audit details."""
        return _PLATFORM_ROLE if self.authority is TakedownOrigin.PLATFORM else _ORG_ROLE


def current_origin(file: MediaFile) -> TakedownOrigin | None:
    """Who holds ``file`` now, or ``None`` if nobody does.

    A file that is quarantined or under legal hold but carries no (or an
    unrecognised) origin reads as ``PLATFORM``: the safe error is to make an
    organization unable to release something, never able to.
    """
    if not (file.is_quarantined or file.legal_hold):
        return None
    try:
        return TakedownOrigin(str(file.takedown_origin))
    except ValueError:
        return TakedownOrigin.PLATFORM


def origin_value(file: MediaFile) -> str | None:
    """:func:`current_origin` as the plain string a response schema carries."""
    origin = current_origin(file)
    return origin.value if origin else None


def _assert_in_tenant(actor: TakedownActor, file: MediaFile) -> None:
    if actor.authority is not TakedownOrigin.ORGANIZATION:
        return
    if actor.organization_id is None or file.organization_id != actor.organization_id:
        # Same answer a missing file gets, so this cannot become an existence oracle.
        raise TakedownRefusedError(CODE_NOT_FOUND, 404, "File not found")


def assert_may_quarantine(actor: TakedownActor, file: MediaFile) -> None:
    """Raise :class:`TakedownRefusedError` unless ``actor`` may take ``file`` down."""
    _assert_in_tenant(actor, file)
    if (
        actor.authority is TakedownOrigin.ORGANIZATION
        and current_origin(file) is TakedownOrigin.PLATFORM
    ):
        raise TakedownRefusedError(
            CODE_PLATFORM_ORIGIN,
            409,
            "This file is held by the service operator and cannot be changed here",
        )


def assert_may_release(actor: TakedownActor, file: MediaFile) -> None:
    """Raise :class:`TakedownRefusedError` unless ``actor`` may release ``file``."""
    _assert_in_tenant(actor, file)
    if (
        actor.authority is TakedownOrigin.ORGANIZATION
        and current_origin(file) is TakedownOrigin.PLATFORM
    ):
        raise TakedownRefusedError(
            CODE_PLATFORM_ORIGIN,
            403,
            "This file is held by the service operator and cannot be released here",
        )


def origin_after_release(file: MediaFile) -> TakedownOrigin | None:
    """The origin to keep once a release has been applied to ``file``.

    Called after the flags are updated: a file that is still held (a release that
    kept the legal hold) keeps whoever held it; a file with nothing left on it has
    no origin.
    """
    if not (file.is_quarantined or file.legal_hold):
        return None
    return current_origin(file)
