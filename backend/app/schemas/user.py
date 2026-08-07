from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import EmailStr
from pydantic import Field
from pydantic import computed_field
from pydantic import field_validator
from pydantic import model_validator

from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.constants import VALID_AUTH_TYPES
from app.schemas.base import UUIDBaseSchema


class UserBrief(BaseModel):
    """Minimal user info for sharing/group contexts."""

    uuid: UUID
    full_name: str | None = None
    email: str

    model_config = ConfigDict(from_attributes=True)


class UserSearchResult(BaseModel):
    """Minimal user info returned by user search endpoint."""

    uuid: UUID
    full_name: str | None = None
    email: str

    model_config = ConfigDict(from_attributes=True)


class UserBase(BaseModel):
    email: EmailStr
    full_name: str | None = None


class UserCreate(UserBase):
    """Schema for creating a new user with password policy validation.

    Password validation is performed against the configured password policy
    when PASSWORD_POLICY_ENABLED is true. The policy enforces:
    - Minimum length (default: 12 characters)
    - Character complexity (uppercase, lowercase, digits, special chars)
    - No user information in password (email username, name parts)

    ``auth_type`` is settable so an admin can pre-provision an LDAP/OIDC/PKI
    account that the external provider matches at first login. Without it every
    admin-created account was ``local`` — and therefore unable to log in at all
    on a deployment that had turned local passwords off.

    ``password`` is required only for ``auth_type == "local"``. An external
    account must NOT carry a local password (``app/auth/utils.py:
    local_password_allowed``), so demanding one would mean storing a credential
    that is, by policy, never accepted.
    """

    password: str | None = Field(default=None, min_length=8)
    role: str | None = "user"
    auth_type: str | None = AUTH_TYPE_LOCAL
    is_active: bool | None = True
    is_superuser: bool | None = False

    @model_validator(mode="after")
    def validate_password_policy(self) -> "UserCreate":
        """Validate the password/auth_type combination against policy.

        This validator runs after all field validators, so we have access
        to both email and full_name for comprehensive validation.

        Raises:
            ValueError: If auth_type is unknown, if a local account has no
                password, or if the password doesn't meet policy requirements.
        """
        from app.auth.password_policy import validate_password

        auth_type = self.auth_type or AUTH_TYPE_LOCAL
        if auth_type not in VALID_AUTH_TYPES:
            raise ValueError(f"Invalid auth_type: {auth_type}")

        if auth_type != AUTH_TYPE_LOCAL:
            # Silently dropping it would leave the caller believing a password
            # was set; the account would then fail every login attempt.
            if self.password:
                raise ValueError(f"auth_type={auth_type!r} accounts do not hold a local password")
            return self

        if not self.password:
            raise ValueError("Password is required for local accounts")

        result = validate_password(
            password=self.password,
            email=self.email,
            full_name=self.full_name,
        )

        if not result.is_valid:
            # Combine all errors into a single message
            error_msg = "; ".join(result.errors)
            raise ValueError(f"Password does not meet policy requirements: {error_msg}")

        return self


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = None
    password: str | None = None
    current_password: str | None = None  # For password change verification
    is_active: bool | None = None
    is_superuser: bool | None = None
    role: str | None = None
    #: super_admin-only (stripped for lesser admins in users.update_user, which
    #: already listed it as privileged before the field existed here).
    auth_type: str | None = None
    allow_local_fallback: bool | None = None

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, v: str | None) -> str | None:
        """Reject an auth_type outside the closed set.

        ``user.auth_type`` is CHECK-constrained since v375, so an unknown value
        would fail at COMMIT with an IntegrityError (a 500) instead of a 422.
        """
        if v is not None and v not in VALID_AUTH_TYPES:
            raise ValueError(f"Invalid auth_type: {v}")
        return v


class UserInDB(UserBase, UUIDBaseSchema):
    """User schema with UUID as public identifier"""

    role: str
    created_at: datetime
    updated_at: datetime
    is_active: bool
    is_superuser: bool
    auth_type: str  # "local", "ldap", "oidc", "pki"
    allow_local_fallback: bool = False
    ldap_uid: str | None = None
    oidc_subject: str | None = None
    pki_subject_dn: str | None = None

    # FedRAMP compliance fields
    password_changed_at: datetime | None = None
    must_change_password: bool = False
    last_login_at: datetime | None = None
    account_expires_at: datetime | None = None

    #: Whether this deployment has proved control of the address (v375). Read by
    #: the admin user list so an unverified account is visible as such.
    email_verified: bool = False
    email_verified_at: datetime | None = None

    #: Administrator admission state (v379): ``pending`` / ``approved`` /
    #: ``rejected``. Served on the ordinary user schema so the admin Users table
    #: shows a held account without a second endpoint, and defaulted so an object
    #: built without the column (tests, the ``get_current_user`` stand-in)
    #: serialises rather than 500s.
    approval_status: str = "approved"
    approved_at: datetime | None = None


class User(UserInDB):
    pass


class Token(BaseModel):
    access_token: str
    token_type: str
    refresh_token: str | None = None
    expires_in: int | None = None  # Access token expiration in seconds


class TokenRefreshRequest(BaseModel):
    """Request body for token refresh endpoint."""

    refresh_token: str | None = None


class TokenPayload(BaseModel):
    sub: str | None = None
    exp: int | None = None
    jti: str | None = None
    role: str | None = None
    type: str | None = None  # 'access' or 'refresh'


# ===== MFA Schemas (FedRAMP IA-2) =====


class MFASetupResponse(BaseModel):
    """Response from MFA setup initiation."""

    secret: str  # Base32-encoded TOTP secret (for manual entry)
    provisioning_uri: str  # otpauth:// URI for QR code
    qr_code_base64: str  # Base64-encoded PNG QR code image


class MFAVerifySetupRequest(BaseModel):
    """Request to verify MFA setup with initial TOTP code."""

    code: str = Field(..., min_length=6, max_length=6, description="6-digit TOTP code")

    @field_validator("code")
    @classmethod
    def validate_code_format(cls, v: str) -> str:
        """Ensure code contains only digits."""
        if not v.isdigit():
            raise ValueError("Code must contain only digits")
        return v


class MFAVerifySetupResponse(BaseModel):
    """Response from successful MFA setup verification.

    The token fields are populated ONLY for forced enrolment — when the call was
    authorized by an enrolment half-token rather than an existing session, completing
    setup also issues the session (matching what /mfa/verify returns). A user who was
    already logged in gets them as None; they already have a session.
    """

    success: bool
    backup_codes: list[str]  # One-time use backup codes (shown only once)
    message: str
    access_token: str | None = None
    token_type: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None


class MFAVerifyRequest(BaseModel):
    """Request to verify MFA code during login."""

    mfa_token: str  # Short-lived token from initial login
    code: str = Field(
        ..., min_length=6, max_length=9, description="6-digit TOTP code or backup code (XXXX-XXXX)"
    )


class MFAVerifyResponse(BaseModel):
    """Response from successful MFA verification during login."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 spec constant, not a password
    refresh_token: str | None = None
    expires_in: int | None = None


class MFADisableRequest(BaseModel):
    """Request to disable MFA for current user."""

    code: str = Field(
        ..., min_length=6, max_length=9, description="6-digit TOTP code or backup code (XXXX-XXXX)"
    )


class MFAStatusResponse(BaseModel):
    """Response indicating MFA status for current user."""

    mfa_enabled: bool
    mfa_configured: bool  # True if user has started MFA setup
    mfa_required: bool  # True if system requires MFA
    can_setup_mfa: bool  # True if user can set up MFA (not PKI/OIDC)


class MFALoginResponse(BaseModel):
    """Response when MFA is required during login."""

    mfa_required: bool = True
    mfa_token: str  # Short-lived token for MFA verification step
    # True when the deployment requires MFA and this user has NOT enrolled: the token is
    # scoped to /mfa/setup + /mfa/verify-setup, not /mfa/verify.
    mfa_enrollment_required: bool = False
    message: str = "MFA verification required"


# ===== Login Banner Schemas (FedRAMP AC-8) =====


class LoginBannerResponse(BaseModel):
    """Response for login banner endpoint."""

    enabled: bool
    text: str
    classification: str
    requires_acknowledgment: bool


class AuthMethodsResponse(BaseModel):
    """What the login page needs in order to render itself correctly.

    This endpoint returned a bare dict, so its contract existed only in a
    hand-maintained TypeScript interface and was invisible to OpenAPI. Every
    field below drives a rendering decision in the SPA.
    """

    methods: list[str]
    oidc_enabled: bool
    pki_enabled: bool
    #: Trusted-header (reverse-proxy) sign-in. The SPA renders one button that POSTs
    #: to ``/auth/proxy/authenticate``; there is nothing for it to collect, because
    #: the proxy has already put the identity on the request.
    proxy_enabled: bool = False
    ldap_enabled: bool
    #: Whether accounts holding a local password may sign in. The password form
    #: stays visible when LDAP is on, because LDAP authenticates through it too.
    local_enabled: bool
    #: Whether to offer a "create an account" link at all.
    allow_registration: bool
    external_providers: list[str]
    mfa_enabled: bool
    mfa_required: bool
    login_banner_enabled: bool
    login_banner_text: str
    login_banner_classification: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def keycloak_enabled(self) -> bool:
        """DEPRECATED duplicate of :attr:`oidc_enabled`.

        A browser holding a cached SPA bundle from before the rename against a
        freshly-upgraded backend is a real deployment state, and that bundle reads
        this key to decide whether to render the SSO button. Emitted for **one minor
        release**; removal ticket: "drop AuthMethodsResponse.keycloak_enabled".

        Computed rather than a second stored field on purpose — the two can never
        report different values, and deletion is one block rather than a hunt for
        every constructor call site.
        """
        return self.oidc_enabled


class BannerAcknowledgmentRequest(BaseModel):
    """Request to acknowledge login banner."""

    # No body required, user info comes from auth token


# ===== Admin Password Reset Schema =====


class AdminPasswordResetRequest(BaseModel):
    """Request body for admin-initiated password reset.

    Moving password from query parameter to request body prevents
    password exposure in server logs, browser history, and referrer headers.
    """

    new_password: str = Field(..., min_length=8, description="New password for the user")
    force_change: bool = Field(
        default=True, description="If true, user must change password on next login"
    )
