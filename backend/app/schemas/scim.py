"""SCIM 2.0 wire contract (RFC 7643 core schema, RFC 7644 protocol).

Two notes for anyone extending this file.

**No aliases, deliberately, and it still conforms.** The repo rule is that wire names
equal Python names, and SCIM's mandatory camelCase attribute names (``userName``,
``externalId``, ``displayName``, ``givenName``) all happen to be valid Python
identifiers. The one attribute that is not — ``$ref`` on a group member — is
*optional* in RFC 7643 §4.2, so it is simply not emitted, and an inbound one is
dropped by pydantic's default ``extra="ignore"``. That last part is load-bearing:
Okta and Entra both send attributes we do not model, and a SCIM server that 400s on
an unmodelled attribute fails their connector tests.

**Requests are lenient, responses are exact.** Inbound models exist to extract the
few values we act on; they do not police the payload. Outbound resources are built
by :func:`user_resource` / :func:`group_resource`, which are the only places the
response shape is decided.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

#: Schema URIs, as RFC 7643 defines them. These strings appear verbatim in every
#: response and are what an IdP's connector matches on.
SCHEMA_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCHEMA_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCHEMA_LIST_RESPONSE = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCHEMA_PATCH_OP = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCHEMA_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
SCHEMA_SERVICE_PROVIDER_CONFIG = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"

#: Default and maximum page size for a list response. The maximum is a ceiling on
#: ``count``, not a suggestion: ``?count=1000000`` is otherwise a free table scan.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


class _Lenient(BaseModel):
    """Base for inbound models: ignore what we do not model (see module docstring)."""

    model_config = ConfigDict(extra="ignore")


class SCIMName(_Lenient):
    """RFC 7643 §4.1.1 ``name`` sub-attribute."""

    formatted: str | None = None
    givenName: str | None = None  # noqa: N815 - SCIM wire name
    familyName: str | None = None  # noqa: N815 - SCIM wire name


class SCIMEmail(_Lenient):
    """RFC 7643 §4.1.2 ``emails`` entry."""

    value: str | None = None
    type: str | None = None
    primary: bool = False


class SCIMMember(_Lenient):
    """A group member reference. ``value`` is the member resource's ``id``."""

    value: str | None = None
    display: str | None = None


class SCIMUserRequest(_Lenient):
    """Inbound ``POST``/``PUT`` body for ``/Users``."""

    schemas: list[str] = Field(default_factory=list)
    userName: str | None = None  # noqa: N815 - SCIM wire name
    externalId: str | None = None  # noqa: N815 - SCIM wire name
    displayName: str | None = None  # noqa: N815 - SCIM wire name
    name: SCIMName | None = None
    emails: list[SCIMEmail] = Field(default_factory=list)
    #: RFC 7643 §4.1.1. Absent on create means "active", which is what every IdP
    #: expects; only an explicit ``false`` deactivates.
    active: bool | None = None

    def resolved_email(self) -> str | None:
        """The address to use, preferring ``userName`` then the primary email.

        Okta sends the address as ``userName``; Entra sends ``userName`` as a UPN and
        the mailbox in ``emails``. Preferring ``userName`` when it looks like an
        address and falling back to the primary email covers both without a
        per-vendor branch.
        """
        if self.userName and "@" in self.userName:
            return self.userName.strip().lower()
        primary = next((e for e in self.emails if e.primary and e.value), None)
        first = next((e for e in self.emails if e.value), None)
        chosen = primary or first
        return chosen.value.strip().lower() if chosen and chosen.value else None

    def resolved_display_name(self) -> str | None:
        """``displayName``, else a name assembled from its parts."""
        if self.displayName:
            return self.displayName.strip()
        if self.name:
            if self.name.formatted:
                return self.name.formatted.strip()
            parts = [p for p in (self.name.givenName, self.name.familyName) if p]
            if parts:
                return " ".join(parts).strip()
        return None


class SCIMGroupRequest(_Lenient):
    """Inbound ``POST``/``PUT`` body for ``/Groups``."""

    schemas: list[str] = Field(default_factory=list)
    displayName: str | None = None  # noqa: N815 - SCIM wire name
    externalId: str | None = None  # noqa: N815 - SCIM wire name
    members: list[SCIMMember] = Field(default_factory=list)


class SCIMPatchOperation(_Lenient):
    """One entry in a ``PatchOp``'s ``Operations`` array (RFC 7644 §3.5.2)."""

    op: str
    path: str | None = None
    #: Untyped on purpose: the value's shape depends entirely on ``path``, and every
    #: consumer in ``api/endpoints/scim/patch_ops.py`` validates what it reads.
    value: Any = None


class SCIMPatchRequest(_Lenient):
    """Inbound ``PATCH`` body."""

    schemas: list[str] = Field(default_factory=list)
    Operations: list[SCIMPatchOperation] = Field(default_factory=list)  # noqa: N815 - SCIM


def _meta(resource_type: str, resource_id: str, created: datetime | None, updated: datetime | None):
    """Build the ``meta`` sub-attribute (RFC 7643 §3.1)."""
    meta: dict[str, Any] = {
        "resourceType": resource_type,
        "location": f"/scim/v2/{resource_type}s/{resource_id}",
    }
    if created is not None:
        meta["created"] = created.isoformat()
    if updated is not None:
        meta["lastModified"] = updated.isoformat()
    return meta


def user_resource(user, *, groups: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Render a ``User`` row as a SCIM User resource.

    ``id`` is the account's **UUID**, never its integer primary key — the same
    hybrid-ID rule the rest of the API follows, and the value an IdP will store and
    send back forever.

    Args:
        user: The ``User`` row.
        groups: Optional pre-resolved group references, each ``{"value", "display"}``.

    Returns:
        The resource body.
    """
    full_name = str(user.full_name or "")
    given, _, family = full_name.partition(" ")
    return {
        "schemas": [SCHEMA_USER],
        "id": str(user.uuid),
        "externalId": user.external_id,
        "userName": user.email,
        "name": {
            "formatted": full_name or None,
            "givenName": given or None,
            "familyName": family or None,
        },
        "displayName": full_name or user.email,
        "emails": [{"value": user.email, "primary": True, "type": "work"}],
        "active": bool(user.is_active),
        "groups": groups or [],
        "meta": _meta("User", str(user.uuid), user.created_at, user.updated_at),
    }


def group_resource(group, *, members: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Render a ``UserGroup`` row as a SCIM Group resource."""
    return {
        "schemas": [SCHEMA_GROUP],
        "id": str(group.uuid),
        "displayName": group.name,
        "members": members or [],
        "meta": _meta("Group", str(group.uuid), group.created_at, group.updated_at),
    }


def list_response(resources: list[dict[str, Any]], *, total: int, start_index: int):
    """Wrap resources in a SCIM ListResponse (RFC 7644 §3.4.2).

    ``startIndex`` is 1-based in SCIM. ``itemsPerPage`` reports what was actually
    returned, not what was asked for.
    """
    return {
        "schemas": [SCHEMA_LIST_RESPONSE],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }
