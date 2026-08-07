"""SCIM 2.0 provisioning surface, mounted at ``/scim/v2`` — **outside ``/api``**.

RFC 7644 §3.1 fixes the base path shape, and every IdP connector concatenates
``/Users`` onto whatever base URL it is given, so the routes live at the URL the
standard describes rather than under this application's ``/api`` prefix. ``main.py``
includes this router directly, the same way ``/metrics`` is mounted at root.

Layout:

- :mod:`auth` — the bearer-token dependency, and why this surface is not rate limited.
- :mod:`errors` — ``urn:ietf:params:scim:api:messages:2.0:Error`` bodies.
- :mod:`filters` — the one supported filter production, and pagination.
- :mod:`patch_ops` — the closed ``PATCH`` set, with the unsupported half named.
- :mod:`users` · :mod:`groups` — the resource endpoints.
- :mod:`discovery` — ``ServiceProviderConfig`` / ``ResourceTypes`` / ``Schemas``.

Writes go through ``services/scim_service.py``, never straight to the ORM: that is
where "deactivation revokes sessions", "never a ``super_admin``" and the audit trail
are enforced, and routing around it is how a provisioning connector would end up with
powers the admin UI does not have.
"""

from fastapi import APIRouter

from app.api.endpoints.scim import discovery
from app.api.endpoints.scim import groups
from app.api.endpoints.scim import users

#: Mounted by ``main.py``. The prefix is part of the standard, not a convention.
router = APIRouter(prefix="/scim/v2", tags=["scim"])
router.include_router(discovery.router)
router.include_router(users.router)
router.include_router(groups.router)

__all__ = ["router"]
