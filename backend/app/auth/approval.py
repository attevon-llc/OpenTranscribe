"""Administrator approval of newly provisioned accounts.

``user.approval_status`` (``v379``) is the single place "may this account be used at
all yet?" is recorded. It is deliberately **not** ``is_active``: deactivation is an
administrator revoking an account that was once usable, approval is an account that
has never been usable, and collapsing the two would make "approve" and "re-enable"
the same button with different consequences.

The three states
----------------
``approved``
    The default, and the value every pre-existing row carries after the migration.
    Nothing about the account is gated.
``pending``
    Assigned at creation time, and only when ``require_account_approval`` is on.
    The account exists, its credential works, and every authenticated request is
    refused with ``detail.code == "account_pending_approval"`` until an
    administrator acts.
``rejected``
    An explicit administrative decision. **The row is not deleted** — the audit
    trail and the email address both have to survive, or the same person simply
    signs up again and looks new.

Enforcement lives in ``api/endpoints/auth/dependencies.py`` alongside the other
account-lifecycle gates (expiry, forced password change, banner acknowledgment),
because that is the one dependency every user-facing route passes through and
because those gates already own the machine-readable ``detail.code`` contract the
SPA branches on. There is no second mechanism.

Turning the setting off
-----------------------
``pending`` stops being enforced — the escape hatch for an operator who enabled the
control, accumulated a queue and changed their mind. ``rejected`` keeps being
enforced, because it was a decision about one account rather than a policy.

The bootstrap super_admin is never pending
------------------------------------------
``initial_data._ensure_admin_user`` writes the column explicitly. It is the
break-glass account: an approval queue that only a signed-in administrator can
clear, on a deployment whose only administrator is in that queue, is a deployment
nobody can get into.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Account is usable. The column default, so anything that does not opt in is
#: created approved — including admin-provisioned accounts, where the admin
#: creating the account *is* the approval.
APPROVAL_APPROVED = "approved"
#: Awaiting an administrator. Assigned only by :func:`initial_approval_status`.
APPROVAL_PENDING = "pending"
#: Refused by an administrator. Never assigned at creation time.
APPROVAL_REJECTED = "rejected"

#: The closed set, mirrored by the ``ck_user_approval_status_valid`` CHECK (v379).
#: The database may never be narrower than this or a write becomes a 500.
VALID_APPROVAL_STATUSES = (APPROVAL_PENDING, APPROVAL_APPROVED, APPROVAL_REJECTED)


def approval_required(db) -> bool:
    """Whether this deployment holds new accounts for approval.

    Args:
        db: Database session, or ``None``/anything unusable — in which case the
            ``.env`` value is used. Resolution is the standard DB > .env > coded
            default via ``DynamicAuthSettings``.

    Returns:
        The effective ``require_account_approval`` value; ``False`` if it cannot
        be resolved, which is the pre-existing behaviour rather than a lockout.
    """
    try:
        if db is None or not hasattr(db, "query"):
            from app.core.auth_settings import get_process_auth_settings

            return bool(get_process_auth_settings().require_account_approval)

        from app.core.auth_settings import get_auth_settings

        return bool(get_auth_settings(db).require_account_approval)
    except Exception:
        logger.debug("Could not resolve require_account_approval; treating as off", exc_info=True)
        return False


def initial_approval_status(db) -> str:
    """Return the ``approval_status`` a freshly provisioned account should carry.

    Call this from self-registration and from every external-IdP JIT creation
    path. Admin-provisioned accounts deliberately do **not** call it: an
    administrator typing the account into the admin UI has already approved it,
    and holding it would mean the admin has to approve their own creation.

    Args:
        db: Database session used to resolve the setting.

    Returns:
        :data:`APPROVAL_PENDING` when approval is required, else
        :data:`APPROVAL_APPROVED`.
    """
    return APPROVAL_PENDING if approval_required(db) else APPROVAL_APPROVED


def is_pending(user) -> bool:
    """Whether *user* is waiting on an administrator.

    Reads defensively: a ``User`` object built without the column (the testing
    stand-in in ``get_current_user``, or a stale ORM class) must not be treated as
    pending, or an unrelated failure becomes a lockout.
    """
    return str(getattr(user, "approval_status", APPROVAL_APPROVED) or "") == APPROVAL_PENDING


def is_rejected(user) -> bool:
    """Whether *user* was refused by an administrator."""
    return str(getattr(user, "approval_status", APPROVAL_APPROVED) or "") == APPROVAL_REJECTED
