"""Wire contract for admin invitations and email verification (v375).

Kept out of ``schemas/user.py`` only to stay under the ~300-line file rule.
Naming follows the newer peripheral convention (``XResponse``).
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import EmailStr
from pydantic import Field
from pydantic import field_validator

from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.constants import VALID_AUTH_TYPES
from app.auth.roles import ROLE_USER
from app.auth.roles import VALID_ROLES


class InvitationCreate(BaseModel):
    """Admin-supplied invitation parameters. No password: that is the point."""

    email: EmailStr
    full_name: str | None = None
    role: str = ROLE_USER
    auth_type: str = AUTH_TYPE_LOCAL
    #: Clamped server-side; a long-lived invite is a long-lived credential.
    expires_in_hours: int = Field(default=72, ge=1, le=336)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"Invalid role: {v}")
        return v

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, v: str) -> str:
        if v not in VALID_AUTH_TYPES:
            raise ValueError(f"Invalid auth_type: {v}")
        return v


class InvitationResponse(BaseModel):
    """An invitation as an admin sees it. The token is never included.

    Only its hash is stored, so it *cannot* be included — the raw token exists
    for exactly the length of the create request, inside the email body.
    """

    uuid: UUID
    email: str
    full_name: str | None = None
    role: str
    auth_type: str
    expires_at: datetime
    created_at: datetime
    used_at: datetime | None = None
    revoked_at: datetime | None = None
    #: Pre-computed for the SPA (fat backend, thin frontend): pending | accepted
    #: | revoked | expired.
    status: str

    model_config = ConfigDict(from_attributes=True)


class InvitationLookupRequest(BaseModel):
    """Public token lookup — what the accept page needs to render itself."""

    token: str = ""


class InvitationLookupResponse(BaseModel):
    """Non-secret facts about an invitation, for the holder of its token.

    Returned only to a caller who already presented the token, so it discloses
    nothing they did not already have. An unknown, used, revoked or expired
    token yields the same generic 400 as :func:`accept_invitation`.
    """

    email: str
    full_name: str | None = None
    auth_type: str
    #: False for ldap/keycloak/pki: the IdP owns the credential, so the accept
    #: page bounces to it instead of showing a password form.
    requires_password: bool
    expires_at: datetime


class InvitationAcceptRequest(BaseModel):
    """Redeem an invitation. ``password`` applies to local invitations only."""

    token: str = ""
    password: str | None = None
    full_name: str | None = None


class InvitationAcceptResponse(BaseModel):
    """What the SPA does next after a successful accept."""

    email: str
    auth_type: str
    #: True when a local password was set and the user can sign in immediately;
    #: False for an external account, which must now go to its IdP.
    can_login_with_password: bool
    message: str


class EmailVerificationRequest(BaseModel):
    """Redeem an email-verification token."""

    token: str = ""


class EmailVerificationResendRequest(BaseModel):
    """Ask for a fresh verification email. Always answered identically."""

    email: str = ""
