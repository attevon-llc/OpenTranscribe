"""Admin external-identity, MFA-reset and user-search routes."""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.lifecycle_gates import _get_client_info
from app.auth.account_linking import emails_agree
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.auth.refusal_audit import audit_user_update_refusal
from app.auth.roles import ROLE_SUPER_ADMIN
from app.core.auth_types import AUTH_TYPE_LOCAL
from app.db.base import get_db
from app.models.user import User
from app.models.user_mfa import UserMFA
from app.schemas.admin import LinkExternalIdentityRequest
from app.schemas.admin import LinkExternalIdentityResponse
from app.schemas.admin import UpdateExternalEmailRequest
from app.schemas.admin import UpdateExternalEmailResponse
from app.services.account_security_service import notify_email_changed
from app.services.account_security_service import revoke_all_sessions

# No basicConfig here — this module is imported via the API router before
# configure_logging() runs; a default root handler would double every log line.
logger = logging.getLogger(__name__)

router = APIRouter()


#: Column each linkable provider's identifier lives on. Mirrors
#: `auth/account_linking.py`'s lookup order (provider id first, email second) —
#: setting this column is what makes a *subsequent* login match here instead of
#: ever reaching the email-match branch that provider's `email_verified` posture
#: might refuse. The three keys are byte-identical to `AUTH_TYPE_OIDC` /
#: `AUTH_TYPE_LDAP` / `AUTH_TYPE_PKI` — `LinkExternalIdentityRequest`'s
#: `_provider_is_linkable` validator restricts `payload.provider` to exactly that
#: set, so `payload.provider` doubles as the auth_type to convert to below, and
#: this map stays total over it. `saml_subject` and `external_id` are deliberately
#: NOT linkable here: SAML has no local test IdP yet (see `auth/CLAUDE.md`), and
#: `external_id` is SCIM's column, which this endpoint has no business writing.
_LINK_IDENTITY_COLUMN = {
    "oidc": "oidc_subject",
    "ldap": "ldap_uid",
    "pki": "pki_subject_dn",
}


@router.put("/users/{user_uuid}/link-identity", response_model=LinkExternalIdentityResponse)
def admin_link_external_identity(
    request: Request,
    user_uuid: str,
    payload: LinkExternalIdentityRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> LinkExternalIdentityResponse:
    """Deliberately link an account to an external identity (P1.3).

    The operator remedy `auth/account_linking.py` documents but that, until this
    endpoint, did not exist: when an IdP cannot assert `email_verified` —
    Authentik hardcodes it `false` for every account — the automatic email-match
    link is refused, by design, and the login just fails. This is the explicit
    alternative: a super_admin sets the provider's own identifier on the
    account, so the identity resolves by that identifier on the very next login
    and the email-match branch (and its refusal) is never reached at all.

    **Linking a `local`-password account converts its `auth_type`, and revokes its
    sessions** (issue #912). Before this, the identifier column was set without
    touching `auth_type`, leaving a `local` account that carried (say) an
    `ldap_uid` — a contradictory state that resolves through the provider-id login
    branch while every other reader of `auth_type` still believed the account
    authenticated with a local password. The conversion is guarded on the
    account's `auth_type` being exactly `local` at the time of the call: an
    already-external `auth_type` (a `pki` account with `allow_local_fallback`
    set, or a downstream registry provider) is left untouched, so this can never
    demote one external method to another. It also deliberately does **not**
    clear `hashed_password` — the login-path conversions do that, but this write
    is effectively irreversible from the API (`assert_password_auth_possible`
    refuses to set a password on a non-local account), so a mistyped identifier
    would strand the admin with no way back. Leaving the hash in place is inert:
    `auth.utils.local_password_allowed` already refuses it unconditionally for
    `ldap` and refuses it for `pki`/`oidc` without the super_admin-only
    `allow_local_fallback` flag.

    A `local` account carrying an external identifier can also arrive here via
    `scim_service.create_user`, which deliberately stamps `external_id`
    without guessing an `auth_type` for the IdP behind a SCIM client — that state
    is legitimate and this endpoint's `_EXTERNAL_IDENTITY_COLUMNS` sibling below
    (`external-email`) does not touch it either.

    Never for a `super_admin` target — that account is local-only by
    architectural invariant, the break-glass account for exactly the IdP that
    might be failing, and linking it to an external identity would make it
    reachable through the login path it exists to survive.
    """
    client_ip, user_agent = _get_client_info(request)

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if str(user.role) == ROLE_SUPER_ADMIN:
        audit_user_update_refusal(
            "link_identity_super_admin_target", user, current_user, client_ip, user_agent
        )
        raise HTTPException(
            status_code=400,
            detail="super_admin accounts are local-only and cannot be linked to an external identity",
        )

    column = _LINK_IDENTITY_COLUMN[payload.provider]
    conflict = (
        db.query(User)
        .filter(getattr(User, column) == payload.identifier)
        .filter(User.id != user.id)
        .first()
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=f"That {payload.provider} identifier is already linked to another account",
        )

    setattr(user, column, payload.identifier)
    previous_auth_type = str(user.auth_type)
    converted = previous_auth_type == AUTH_TYPE_LOCAL
    if converted:
        # payload.provider IS the auth-type string — the schema's validator closes
        # it to exactly {oidc, ldap, pki}, which are AUTH_TYPE_OIDC/_LDAP/_PKI
        # verbatim (see _LINK_IDENTITY_COLUMN's comment).
        user.auth_type = payload.provider
        revoke_all_sessions(db, user, reason="external identity linked")
    db.commit()

    logger.info(
        "super_admin %s linked %s identity %s to user %s",
        current_user.email,
        payload.provider,
        payload.identifier,
        user.email,
    )
    audit_logger.log(
        event_type=AuditEventType.ADMIN_USER_UPDATE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        target_user_id=int(user.id),
        target_username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={
            "action": "link_external_identity",
            "target_user": user_uuid,
            "provider": payload.provider,
            "previous_auth_type": previous_auth_type,
            "auth_type": str(user.auth_type),
        },
    )

    return LinkExternalIdentityResponse(
        success=True,
        provider=payload.provider,
        identifier=payload.identifier,
        auth_type=str(user.auth_type),
    )


#: The identifier column each external method stamps on an account. A row carrying
#: any of these is "already linked" — which is what makes the email remedy below a
#: REMEDY rather than a general "edit anyone's login address" power. NOT the same
#: claim as "already linked implies non-local", though: `scim_service.create_user`
#: deliberately provisions `auth_type='local'` rows carrying `external_id` (it has
#: no basis for guessing which IdP a SCIM client speaks for), and that state is
#: legitimate — this tuple does not declare it illegal. The handler below adds a
#: SEPARATE, narrower `auth_type == local` check for exactly that reason (#912).
_EXTERNAL_IDENTITY_COLUMNS = (
    "oidc_subject",
    "ldap_uid",
    "pki_subject_dn",
    "saml_subject",
    "external_id",
)


@router.put("/users/{user_uuid}/external-email", response_model=UpdateExternalEmailResponse)
def admin_update_external_email(
    request: Request,
    user_uuid: str,
    payload: UpdateExternalEmailRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> UpdateExternalEmailResponse:
    """Accept an IdP's new address for an already-linked account (issue #867).

    `auth/account_linking.assert_provider_id_link_permitted` refuses a login whose
    asserted email no longer agrees with the stored one — the signal that a recycled
    `sub`/`uid`/DN may now belong to a different real person. That check is right, but
    it sits **before** every provider's profile-refresh code, so the stored address can
    never catch up on its own: a benign IdP-side rename (a marriage, a domain
    migration, a corrected typo) became a permanent lockout with no path back short of
    direct SQL. This is that path.

    **Why an explicit admin action rather than auto-accepting the new address.** At the
    point of the check the two cases are indistinguishable — a legitimate rename and an
    identifier reassigned to somebody else look identical, because in both the source
    asserts an address the account does not hold. Auto-accepting resolves that ambiguity
    in the attacker's favour, which is the vector `account_linking` was written to close.
    A human comparing the two addresses can tell them apart; the refusal's audit record
    carries both for exactly that purpose.

    **Deliberately narrow.** It refuses an account that carries no external identifier
    at all, so it is a remedy for an IdP-linked account and not a general
    "rewrite anyone's login email" power — for a `local` account the email *is* the
    credential identity, and moving it is a different decision that this endpoint is not.
    Never a `super_admin`, matching `link-identity`: that account is local-only and is
    the break-glass for exactly the IdP that might be failing.

    **A second, narrower precondition (issue #912):** an `auth_type == local` account
    is refused even if it carries one of `_EXTERNAL_IDENTITY_COLUMNS`. Before #912's
    fix to `link-identity`, that endpoint could leave exactly this state behind (an
    external identifier stamped on a still-`local` account); it no longer can, but a
    SCIM-provisioned account (`scim_service.create_user`) legitimately still
    carries `auth_type='local'` and `external_id` together, by explicit design in that
    module. This check is a plain equality against `AUTH_TYPE_LOCAL`, never a
    membership test against the valid-auth-type set — a downstream registry provider's
    account (`auth_type='managed_idp'` or similar) must keep reaching this remedy, and
    a closed allow-list here would plant a second hardcoded set beside issue #866's.

    Sessions are revoked, because the address is what the account authenticates as.
    """
    client_ip, user_agent = _get_client_info(request)

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if str(user.role) == ROLE_SUPER_ADMIN:
        audit_user_update_refusal(
            "external_email_super_admin_target", user, current_user, client_ip, user_agent
        )
        raise HTTPException(
            status_code=400,
            detail="super_admin accounts are local-only and cannot be linked to an external identity",
        )

    if not any(getattr(user, column, None) for column in _EXTERNAL_IDENTITY_COLUMNS):
        audit_user_update_refusal(
            "external_email_no_external_identity", user, current_user, client_ip, user_agent
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "This account is not linked to an external identity. This endpoint accepts an "
                "identity provider's updated address for an already-linked account; it is not a "
                "general email change."
            ),
        )

    if str(user.auth_type) == AUTH_TYPE_LOCAL:
        audit_user_update_refusal(
            "external_email_local_account", user, current_user, client_ip, user_agent
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "This account authenticates with a local password, so its email address is its "
                "own login credential — not an address an identity provider owns. This endpoint "
                "accepts an IdP's updated address for an externally-authenticated account. "
                "Change the account's auth_type first if that is what it should be."
            ),
        )

    new_email = payload.email.strip()
    previous_email = str(user.email)
    if emails_agree(new_email, previous_email):
        # Idempotent: re-applying the remedy must not churn sessions for nothing.
        return UpdateExternalEmailResponse(
            success=True, email=previous_email, previous_email=previous_email
        )

    # Case-INSENSITIVE, deliberately stricter than the byte-exact unique index on
    # `user.email`: `emails_agree` treats `Alice@x` and `alice@x` as one mailbox, so
    # letting two rows hold both would make the account a login resolves to depend on
    # which comparison ran. Refusing is the safe direction; the admin can pick another.
    conflict = (
        db.query(User)
        .filter(func.lower(User.email) == new_email.lower())
        .filter(User.id != user.id)
        .first()
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail="That email address is already in use by another account",
        )

    user.email = new_email
    # email_verified is proof THIS deployment mailed the address on the row — a
    # property of the ADDRESS, so it cannot survive the address changing (#909).
    # Every account that reaches this line is now non-local (the auth_type==local
    # check above refuses the rest, #912), and assert_email_verified_for_local_login
    # returns early for a non-local account — so clearing this flag is not a live
    # login gate for anyone who reaches here today. It is still the honest record
    # rather than a no-op: it matters the moment the account is ever converted back
    # to local (or if a future local-fallback path starts reading it), and leaving a
    # stale True on an address the deployment never actually verified is exactly the
    # kind of "looks fine, isn't" state this module exists to avoid. Deliberately no
    # re-verification token is issued here (unlike the self-service path in
    # users.py): this endpoint is an administrator accepting an IdP's assertion for
    # an already-linked account, and mailing an IdP-owned address unprompted isn't
    # its job — issue_verification_token would also no-op for a genuinely external
    # account.
    user.email_verified = False
    user.email_verified_at = None
    # Same shape as the self-service email change in `users.py`: revoke IN the
    # transaction so a commit failure rolls the revocation back with it. An email
    # change is an identity change on an account that authenticates by it, and
    # `auth/CLAUDE.md` makes revocation non-optional for that class of change.
    revoke_all_sessions(db, user, reason="external email change")
    db.commit()

    logger.info(
        "super_admin %s accepted a new external email for user %s (was %s)",
        current_user.email,
        user_uuid,
        previous_email,
    )
    audit_logger.log(
        event_type=AuditEventType.ADMIN_USER_UPDATE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        target_user_id=int(user.id),
        target_username=new_email,
        source_ip=client_ip,
        user_agent=user_agent,
        details={
            "action": "update_external_email",
            "target_user": user_uuid,
            "previous_email": previous_email,
        },
    )
    # Best-effort, and it may well bounce (a dead domain is one of the triggers) — but
    # a silent address change is the first half of an account takeover, so the previous
    # address is told, exactly as the self-service change in `users.py` does.
    notify_email_changed(previous_email, new_email)

    return UpdateExternalEmailResponse(success=True, email=new_email, previous_email=previous_email)


# ============== MFA Management ==============


@router.post("/users/{user_uuid}/mfa/reset")
def admin_reset_user_mfa(
    request: Request,
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """Admin reset of user MFA (if user loses device). Requires super_admin role.

    The audit record reports what actually happened. It used to sit OUTSIDE the
    ``if mfa_settings:`` block and always log ``MFA_DISABLE`` / ``SUCCESS``, so a
    reset against an account with no second factor enrolled recorded a disable
    that did nothing — an event a reviewer would read as "this account's MFA was
    removed on this date". The attempt is still recorded either way (a run of
    resets against accounts that have no MFA is itself worth seeing); only the
    outcome changes.
    """
    client_ip, user_agent = _get_client_info(request)

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    mfa_settings = db.query(UserMFA).filter(UserMFA.user_id == user.id).first()
    # A row that exists with `totp_enabled` already false is the same non-event as
    # no row at all: nothing was in force, so nothing was disabled.
    was_enabled = bool(mfa_settings and mfa_settings.totp_enabled)

    if mfa_settings:
        # DELETE the row, like the user-facing `POST /auth/mfa/disable` does.
        # This branch used to null `totp_secret`, which `v200` made NOT NULL — so
        # the reset raised an IntegrityError (a 500) for every account that
        # actually had a second factor, i.e. the only case the endpoint exists
        # for. Nothing caught it because the audit call, and the `{"success":
        # true}` response, both sat outside this block: an account with no MFA
        # took the no-op path and answered 200.
        db.delete(mfa_settings)
        # Dropping the second factor must not leave sessions that were minted
        # while it was still in force.
        revoke_all_sessions(db, user, reason="admin MFA reset")
        db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_MFA_DISABLE,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        outcome=AuditOutcome.SUCCESS if was_enabled else AuditOutcome.FAILURE,
        error_code=None if was_enabled else "MFA_NOT_ENROLLED",
        details={
            "target_user": user_uuid,
            "reset_by": "admin",
            "mfa_was_enabled": was_enabled,
        },
    )

    return {"success": True}


# ============== User Search ==============


@router.get("/users/search")
def admin_search_users(
    query: str | None = Query(None, description="Search query for email or name"),
    role: str | None = Query(None, description="Filter by role"),
    auth_type: str | None = Query(None, description="Filter by auth type"),
    is_active: bool | None = Query(None, description="Filter by active status"),
    limit: int = Query(default=50, le=200, description="Maximum results to return"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Advanced user search with filtering and pagination."""
    from app.utils.pagination import paginate

    q = db.query(User)

    if query:
        q = q.filter(or_(User.email.ilike(f"%{query}%"), User.full_name.ilike(f"%{query}%")))

    if role:
        q = q.filter(User.role == role)

    if auth_type:
        q = q.filter(User.auth_type == auth_type)

    if is_active is not None:
        q = q.filter(User.is_active == is_active)

    users, total = paginate(q, offset, limit)

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "users": [
            {
                "uuid": str(u.uuid),
                "email": u.email,
                "full_name": u.full_name,
                "role": u.role,
                "auth_type": u.auth_type,
                "is_active": u.is_active,
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
    }
