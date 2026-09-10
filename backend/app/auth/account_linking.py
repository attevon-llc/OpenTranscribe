"""One rule for "may this external identity take over an existing account?".

Update (provider-ID branch, GH lane3-account-linking): the module below originally
covered only the **email-match** branch, on the stated assumption that a stored
provider identifier (``ldap_uid`` / ``oidc_subject`` / ``saml_subject`` /
``pki_subject_dn`` / ``external_id``) was only ever set by a deliberate admin action
via the link-identity endpoint, and so needed no re-litigation on login. That
assumption is false: every JIT-provisioning path (LDAP, OIDC, SAML, PKI, and the
registry-based external seam) also stamps that same identifier automatically on a
user's *first* login. If that identifier is later reused or reassigned by a
different real-world person authenticating through the same external method — a
recycled LDAP uid, a replayed OIDC ``sub`` from a lower-assurance IdP, a reissued
certificate subject DN — the provider-ID branch matched and logged that person in
as the ORIGINAL owner, with none of the protections below. ``assert_provider_id_
link_permitted`` closes that gap with the same super_admin protection, plus a
narrower corroboration check: the source's asserted email (when it asserts one at
all) must still agree with the account's stored email, or the match is refused.


Every external auth path (LDAP, PKI, and the registry-based JIT seam in
``external_sync.py``) resolves a user by provider id first and then, if that misses,
falls back to matching ``User.email``. That fallback is the account-takeover vector:
the email is an **attribute of the external source**, so whoever can write it —
a directory administrator setting ``mail``, a self-service directory where users edit
their own address, or anyone who can get a certificate issued — can point it at an
existing account and inherit it, including its content and its privileges.

``external_sync.sync_external_user_to_db`` already established the rule for the cloud
JIT seam and ``constants.CLOUD_SEAM_VERSION`` (v2) documents it:

1. link only when **the source asserts the address is verified**, and
2. **never** link a ``super_admin`` account by email, ever.

This module applies the same rule to the two paths that were still missing it. It is
the single implementation; do not add a third.

What happens on refusal
-----------------------
The **login fails**. Specifically it does *not* fall through to "create a new
account": the refusal happens precisely when an account with that email already
exists, so creating one would either collide on the unique email index or leave two
accounts for one person — a silent, confusing duplicate is a worse outcome than a
refused login, and it would also hand the attacker a fresh account with the victim's
address on it.

The refusal is audited as ``AUTH_LOGIN_FAILURE`` with ``error_code
ACCOUNT_LINK_REFUSED`` so it is greppable, and surfaces to the caller as the *same*
generic failure that path returns for a bad credential — a distinct error would tell
an attacker "that address exists and is privileged", which is exactly what the guard
is protecting.

Operator remedy: link the account deliberately instead of by email coincidence —
``PUT /api/admin/users/{uuid}/link-identity`` (super_admin) sets the account's
provider identifier (``oidc_subject`` / ``ldap_uid`` / ``pki_subject_dn``) directly,
so the *next* login matches by that identifier and never reaches this branch at
all. That is a decision an administrator makes, not one an external directory
makes on its own — and it is also the fix for a source that can never assert
``email_verified`` in the first place (Authentik hardcodes it ``false`` for every
account; see the endpoint's docstring).

Two refusals, two remedies (issue #867)
---------------------------------------
The provider-ID branch below needs a *different* remedy, and for a long time it had
none at all — which made an ordinary IdP-side email change a **permanent lockout**.
``assert_provider_id_link_permitted`` refuses when the source's asserted email no
longer agrees with the stored one, and it runs **before** every provider's
profile-refresh code (``oidc/provisioning._update_oidc_user`` and its four siblings),
so the stored address can never catch up on its own. The self-heal
"log in successfully and your profile updates" is unreachable by construction: every
retry re-runs the same sequence to the same 401.

| Refusal | What is wrong | Remedy |
|---|---|---|
| email-match branch | the account carries no provider identifier | ``PUT /api/admin/users/{uuid}/link-identity`` |
| provider-ID branch | the identifier matches, the stored email is stale | ``PUT /api/admin/users/{uuid}/external-email`` |

Both are super_admin, both audited, both refuse a ``super_admin`` target, and both
exist because the alternative — trusting whatever the source asserts — is the
takeover this module was written to refuse. The second is deliberately narrow: it
refuses an account carrying no external identifier at all, so it is a remedy for a
linked identity and not a general "rewrite anyone's login email" power.
"""

import logging

from fastapi import HTTPException
from fastapi import status

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.roles import ROLE_SUPER_ADMIN

logger = logging.getLogger(__name__)

#: Audit ``error_code`` for a refused email-match link.
LINK_REFUSED_ERROR_CODE = "ACCOUNT_LINK_REFUSED"


def emails_agree(asserted: str | None, stored: str | None) -> bool:
    """Whether an externally-asserted address names the same mailbox as a stored one.

    Case- and whitespace-insensitive, because neither carries identity: an IdP that
    re-cases ``Alice@Example.com`` is not asserting a different person, and treating
    it as one locked the account out forever (issue #867 — the refusal happens before
    every provider's profile refresh, so the stored value can never catch up).

    Deliberately the same comparison
    ``api/endpoints/auth/dependencies._enforce_proxy_identity_consistency`` already
    makes for the same question; this is that rule named, not a second one. Two
    absent values do **not** agree — a caller with nothing to compare must take its
    own fail-open decision explicitly rather than inherit one from here.
    """
    if not asserted or not stored:
        return False
    return asserted.strip().lower() == stored.strip().lower()


def assert_email_link_permitted(
    user,
    *,
    provider: str,
    source_identifier: str,
    email_verified: bool,
    failure_detail: str,
    failure_headers: dict[str, str] | None = None,
) -> None:
    """Refuse an email-matched link that the source is not entitled to make.

    Call this **only** on the email-match branch. An account already carrying the
    provider's own identifier (``ldap_uid``, ``pki_subject_dn``, ``external_id``) is
    not re-litigated by *this* function — but it is not exempt from a guard
    entirely: see ``assert_provider_id_link_permitted`` below, which covers that
    branch on its own, narrower terms.

    Args:
        user: The pre-existing ``User`` row that the email matched.
        provider: Auth-type string of the external source, for the log/audit record.
        source_identifier: The external identity being asserted (LDAP uid, subject DN).
        email_verified: Whether the source asserts this address as verified. Fails
            closed: pass ``False`` whenever the source has no verified-address concept.
        failure_detail: The ``HTTPException`` detail to raise. Must be byte-identical
            to what this auth path returns for an ordinary credential failure.
        failure_headers: Optional response headers to match that same failure.

    Raises:
        HTTPException: 401, when the link is refused.
    """
    reason: str | None = None
    if str(getattr(user, "role", "")) == ROLE_SUPER_ADMIN:
        # Unconditional, and checked first: a platform-owner account is never
        # acquired through an external directory, verified address or not.
        reason = "super_admin_never_linked"
    elif not email_verified:
        reason = "email_not_verified"

    if reason is None:
        return

    logger.warning(
        "SECURITY: refusing to link %s identity %s to existing account %s (%s)",
        provider,
        source_identifier,
        getattr(user, "email", "?"),
        reason,
    )
    audit_logger.log(
        event_type=AuditEventType.AUTH_LOGIN_FAILURE,
        outcome=AuditOutcome.FAILURE,
        user_id=getattr(user, "id", None),
        username=source_identifier,
        error_code=LINK_REFUSED_ERROR_CODE,
        details={
            "auth_method": provider,
            "reason": reason,
            "matched_by": "email",
        },
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=failure_detail,
        headers=failure_headers,
    )


def assert_provider_id_link_permitted(
    user,
    *,
    provider: str,
    source_identifier: str,
    asserted_email: str | None,
    failure_detail: str,
    failure_headers: dict[str, str] | None = None,
) -> None:
    """Refuse a provider-ID-matched login that cannot be corroborated.

    Call this on the **provider-ID-match branch** (``ldap_uid`` / ``oidc_subject`` /
    ``saml_subject`` / ``pki_subject_dn`` / ``external_id``), right after that lookup
    finds a row and before any privilege or profile field is touched. This branch used
    to carry no guard at all, on the assumption that the stored identifier was only
    ever set by a deliberate admin action (see the module docstring for why that
    assumption is false: JIT provisioning stamps the same identifier on ordinary
    first logins too).

    Two checks, both unconditional — the same shape as ``assert_email_link_permitted``,
    applied on this branch's own terms:

    1. **super_admin is never matched by provider ID either.** A platform-owner
       account is never acquired through an external directory, whichever column
       matched.
    2. **The match must be corroborated by email.** When the source asserts an
       email at all, it must agree with the account's stored address. A stored
       identifier can go stale or be reassigned (a directory recycles a uid, an
       IdP replays a ``sub`` from a different tenant, a certificate subject DN is
       reissued to someone else) — if the *person* behind the identifier has
       changed, their asserted email will no longer match the account it used to
       belong to, and that divergence is exactly the signal this check acts on.
       A source asserting **no** email at all is passed through unchanged: many
       directories legitimately omit it on some calls, and requiring one here
       would break the ordinary "same identifier, same person" case this
       function must otherwise leave alone — the same fail-open-on-absence shape
       already used by the header-trust groups check elsewhere in this package.

    ⚠️ **The comparison is normalised** (``.strip().lower()``), matching
    ``api/endpoints/auth/dependencies._enforce_proxy_identity_consistency``, which
    already compares an asserted address to a stored one that way. It was an exact
    byte compare, so an IdP re-casing an address asserted a *different person* as
    far as this function was concerned and locked the account out permanently
    (issue #867). Two different people never differ only by case, so this narrows
    the false-positive surface without widening the true one.

    **Refusal is not recoverable by logging in again, by design** — the gate sits
    *before* every provider's profile-refresh code, so the stored email cannot
    catch up on its own; making it self-heal would auto-accept whatever the source
    asserts, which is exactly the takeover this guard exists to refuse. The remedy
    is deliberate and administrative: ``PUT /api/admin/users/{uuid}/external-email``
    (super_admin), the sibling of the ``link-identity`` remedy above. The audit
    record below therefore carries **both** addresses under
    ``provider_id_email_mismatch``, because deciding between "a legitimate rename"
    and "a recycled identifier now held by someone else" is the whole judgement
    being escalated to a human, and it cannot be made from one address.

    Args:
        user: The pre-existing ``User`` row the provider identifier matched.
        provider: Auth-type string of the external source, for the log/audit record.
        source_identifier: The provider identifier that matched (LDAP uid, OIDC
            subject, SAML subject, PKI subject DN, or external id).
        asserted_email: The email address this login asserts, or ``None``/empty
            when the source asserts none.
        failure_detail: The ``HTTPException`` detail to raise. Must be byte-identical
            to what this auth path returns for an ordinary credential failure.
        failure_headers: Optional response headers to match that same failure.

    Raises:
        HTTPException: 401, when the match is refused.
    """
    reason: str | None = None
    if str(getattr(user, "role", "")) == ROLE_SUPER_ADMIN:
        reason = "super_admin_never_linked"
    elif asserted_email and not emails_agree(asserted_email, getattr(user, "email", None)):
        reason = "provider_id_email_mismatch"

    if reason is None:
        return

    logger.warning(
        "SECURITY: refusing provider-id match for %s identity %s against existing account %s (%s)",
        provider,
        source_identifier,
        getattr(user, "email", "?"),
        reason,
    )
    details = {
        "auth_method": provider,
        "reason": reason,
        "matched_by": "provider_id",
    }
    if reason == "provider_id_email_mismatch":
        # Both addresses, because the operator's decision — legitimate rename vs
        # reassigned identifier — cannot be made from either one alone. See the
        # docstring; the remedy is PUT /api/admin/users/{uuid}/external-email.
        details["asserted_email"] = asserted_email or ""
        details["stored_email"] = str(getattr(user, "email", "") or "")
    audit_logger.log(
        event_type=AuditEventType.AUTH_LOGIN_FAILURE,
        outcome=AuditOutcome.FAILURE,
        user_id=getattr(user, "id", None),
        username=source_identifier,
        error_code=LINK_REFUSED_ERROR_CODE,
        details=details,
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=failure_detail,
        headers=failure_headers,
    )
