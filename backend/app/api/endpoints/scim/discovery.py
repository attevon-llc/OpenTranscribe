"""``ServiceProviderConfig``, ``ResourceTypes`` and ``Schemas`` (RFC 7644 §4).

An IdP reads these before it provisions anything, and it believes them. So the
capability flags here are written from what ``users.py`` / ``groups.py`` /
``patch_ops.py`` actually do — ``filter.supported`` is true because one filter
production works, and ``documentationUri`` points at the prose listing exactly which
one. Advertising ``sort`` or ``bulk`` we do not implement would make a connector
issue requests that 400 for reasons its administrator cannot see.

These three routes are authenticated like everything else here. RFC 7644 §4 allows
them to be anonymous; they are not, because they disclose the deployment's
provisioning topology and there is no client that needs them before it has a token.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends

from app.api.endpoints.scim.auth import require_scim_token
from app.api.endpoints.scim.errors import not_found
from app.api.endpoints.scim.users import _scim_json
from app.models.scim_token import SCIMToken
from app.schemas.scim import MAX_PAGE_SIZE
from app.schemas.scim import SCHEMA_GROUP
from app.schemas.scim import SCHEMA_LIST_RESPONSE
from app.schemas.scim import SCHEMA_SERVICE_PROVIDER_CONFIG
from app.schemas.scim import SCHEMA_USER

router = APIRouter()

#: Where the supported filter and PATCH subsets are written down in full.
#:
#: Deliberately the source tree rather than a prose page: ``filters.py`` and
#: ``patch_ops.py`` ARE the specification of what this server accepts, and their
#: module docstrings list the unsupported half by name. A ``documentationUri`` an
#: administrator follows to a 404 — or to a page that has drifted from the code — is
#: worse than none, and this one cannot drift. Repoint it at ``docs/SCIM_SETUP.md``
#: when that page exists.
DOCS_URI = (
    "https://github.com/attevon-llc/OpenTranscribe/tree/master/backend/app/api/endpoints/scim"
)

_SERVICE_PROVIDER_CONFIG = {
    "schemas": [SCHEMA_SERVICE_PROVIDER_CONFIG],
    "documentationUri": DOCS_URI,
    # PATCH is supported for a CLOSED set of paths; api/endpoints/scim/patch_ops.py
    # refuses everything else with 400 invalidPath rather than accepting and ignoring.
    "patch": {"supported": True},
    # No bulk endpoint. Advertising one an IdP could not use is worse than saying no.
    "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
    # One production only: '<attribute> eq "<value>"'. See scim/filters.py.
    "filter": {"supported": True, "maxResults": MAX_PAGE_SIZE},
    # Passwords are never set through SCIM: a provisioned account has no local
    # password at all and authenticates through the deployment's IdP.
    "changePassword": {"supported": False},
    "sort": {"supported": False},
    "etag": {"supported": False},
    "authenticationSchemes": [
        {
            "type": "oauthbearertoken",
            "name": "OAuth Bearer Token",
            "description": (
                "A token issued by a super_admin at Settings -> Authentication -> SCIM "
                "and presented as 'Authorization: Bearer ...'."
            ),
            "specUri": "https://www.rfc-editor.org/rfc/rfc6750",
            "primary": True,
        }
    ],
    "meta": {"resourceType": "ServiceProviderConfig", "location": "/scim/v2/ServiceProviderConfig"},
}

_RESOURCE_TYPES = [
    {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User",
        "name": "User",
        "endpoint": "/Users",
        "description": "User Account",
        "schema": SCHEMA_USER,
        "schemaExtensions": [],
        "meta": {"resourceType": "ResourceType", "location": "/scim/v2/ResourceTypes/User"},
    },
    {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "Group",
        "name": "Group",
        "endpoint": "/Groups",
        "description": "Group",
        "schema": SCHEMA_GROUP,
        "schemaExtensions": [],
        "meta": {"resourceType": "ResourceType", "location": "/scim/v2/ResourceTypes/Group"},
    },
]


def _attribute(name: str, *, multi: bool = False, sub: list[str] | None = None) -> dict:
    """One attribute definition, at the detail level a connector actually reads."""
    definition: dict = {
        "name": name,
        "type": "complex" if sub else ("boolean" if name == "active" else "string"),
        "multiValued": multi,
        "required": name == "userName",
        "caseExact": False,
        "mutability": "readWrite",
        "returned": "default",
        "uniqueness": "server" if name == "userName" else "none",
    }
    if sub:
        definition["subAttributes"] = [{"name": s, "type": "string"} for s in sub]
    return definition


_SCHEMAS = [
    {
        "id": SCHEMA_USER,
        "name": "User",
        "description": "User Account",
        "attributes": [
            _attribute("userName"),
            _attribute("displayName"),
            _attribute("externalId"),
            _attribute("active"),
            _attribute("name", sub=["formatted", "givenName", "familyName"]),
            _attribute("emails", multi=True, sub=["value", "type", "primary"]),
        ],
        "meta": {"resourceType": "Schema", "location": f"/scim/v2/Schemas/{SCHEMA_USER}"},
    },
    {
        "id": SCHEMA_GROUP,
        "name": "Group",
        "description": "Group",
        "attributes": [
            _attribute("displayName"),
            _attribute("members", multi=True, sub=["value", "display"]),
        ],
        "meta": {"resourceType": "Schema", "location": f"/scim/v2/Schemas/{SCHEMA_GROUP}"},
    },
]


@router.get("/ServiceProviderConfig")
def service_provider_config(_token: SCIMToken = Depends(require_scim_token)):
    """Report what this server supports — see the module docstring on honesty."""
    return _scim_json(_SERVICE_PROVIDER_CONFIG)


@router.get("/ResourceTypes")
def resource_types(_token: SCIMToken = Depends(require_scim_token)):
    """The two resource types this server exposes."""
    return _scim_json(
        {
            "schemas": [SCHEMA_LIST_RESPONSE],
            "totalResults": len(_RESOURCE_TYPES),
            "startIndex": 1,
            "itemsPerPage": len(_RESOURCE_TYPES),
            "Resources": _RESOURCE_TYPES,
        }
    )


@router.get("/ResourceTypes/{resource_id}")
def resource_type(resource_id: str, _token: SCIMToken = Depends(require_scim_token)):
    """One resource type by id (``User`` / ``Group``)."""
    match = next((r for r in _RESOURCE_TYPES if r["id"] == resource_id), None)
    if match is None:
        raise not_found("ResourceType", resource_id)
    return _scim_json(match)


@router.get("/Schemas")
def schemas(_token: SCIMToken = Depends(require_scim_token)):
    """The core User and Group schemas, as this server implements them."""
    return _scim_json(
        {
            "schemas": [SCHEMA_LIST_RESPONSE],
            "totalResults": len(_SCHEMAS),
            "startIndex": 1,
            "itemsPerPage": len(_SCHEMAS),
            "Resources": _SCHEMAS,
        }
    )


@router.get("/Schemas/{schema_id}")
def schema_by_id(schema_id: str, _token: SCIMToken = Depends(require_scim_token)):
    """One schema by its URN (RFC 7644 §4).

    Consumed by an external SCIM client — an IdP's provisioning connector fetching
    the attribute definition for ``urn:ietf:params:scim:schemas:core:2.0:User`` or
    ``:Group`` before it maps its own directory fields. Never called by the SPA.

    Authorized by ``require_scim_token`` (a Bearer SCIM token, not a session), like
    every other route in this package — see the module docstring on why these are not
    anonymous even though the RFC permits it.

    ``schema_id`` is matched against the served ``_SCHEMAS`` literally, so it must be
    the full URN, not a short name; a miss raises a SCIM ``Error`` resource via
    ``not_found``, not FastAPI's default 404 body.
    """
    match = next((s for s in _SCHEMAS if s["id"] == schema_id), None)
    if match is None:
        raise not_found("Schema", schema_id)
    return _scim_json(match)
