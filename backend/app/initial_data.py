"""
Initial data setup for OpenTranscribe.

Seeds the database with essential data on first startup:
- Admin user account
- Default tags
- System default summary prompts

Called from FastAPI lifespan after migrations complete.
All operations are idempotent (safe to run multiple times).
"""

import logging
import secrets
from datetime import UTC
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.approval import APPROVAL_APPROVED
from app.auth.password_history import add_password_to_history
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import role_implies_superuser
from app.core.security import get_password_hash
from app.db.base import get_db
from app.models.media import Tag
from app.models.prompt import SummaryPrompt
from app.models.user import User
from app.services.tag_service import normalize_tag_name

# No module-level basicConfig: this module is imported during app startup and
# a default root handler here would double every log line. configure_logging()
# (app.core.logging_config) owns the root handler.
logger = logging.getLogger(__name__)


#: The well-known development credential. NEVER seeded in a hardened environment.
_DEV_ADMIN_EMAIL = "admin@example.com"
_DEV_ADMIN_PASSWORD = "password"  # noqa: S105  # nosec B105  # gitleaks:allow


def _resolve_bootstrap_admin() -> tuple[str, str, bool]:
    """Decide which admin credential to seed.

    Returns:
        ``(email, password, generated)`` — *generated* is True when the password was
        randomly generated and must be surfaced to the operator exactly once.
    """
    from app.core.config import settings

    if not settings.is_hardened:
        # Dev/test: the well-known credential the e2e suite and local workflow expect.
        return _DEV_ADMIN_EMAIL, _DEV_ADMIN_PASSWORD, False

    if settings.INITIAL_ADMIN_PASSWORD:
        return settings.INITIAL_ADMIN_EMAIL, settings.INITIAL_ADMIN_PASSWORD, False

    # Hardened with no password supplied: generate one rather than shipping a known
    # default. token_urlsafe(24) is 192 bits of entropy.
    return settings.INITIAL_ADMIN_EMAIL, secrets.token_urlsafe(24), True


def _ensure_admin_user(db: Session) -> None:
    """Create the bootstrap admin user if no admin exists yet.

    The bootstrap admin is the platform owner, so it gets ``super_admin`` (which
    can configure authentication, change roles, etc.). ``is_superuser`` is the
    derived mirror of ``role == super_admin`` — see ``app.auth.roles``.

    **The hardcoded dev super_admin credential is only ever created in
    a relaxed environment** (issue #284 A0.9). It used to be seeded unconditionally on
    every boot with no env gate, so any public deployment shipped with a known
    super-admin credential. A hardened deployment instead gets ``INITIAL_ADMIN_EMAIL``
    with ``INITIAL_ADMIN_PASSWORD``, or a generated password logged once at startup.

    The seeded password goes into ``password_history`` like every other
    account-creation path's does, and a *generated* one additionally sets
    ``must_change_password`` — see the comments at the write below.

    Repairing an existing account whose derived mirror drifted is
    :func:`_heal_superuser_mirror`'s job, not this function's.
    """
    email, password, generated = _resolve_bootstrap_admin()

    user = db.query(User).filter(User.email == email).first()

    # In a hardened environment, never resurrect the dev credential — and don't create
    # a second bootstrap admin if the operator already has one under another address.
    if not user and _admin_exists(db):
        logger.debug("An admin already exists; skipping bootstrap admin creation")
        return

    if not user:
        password_hash = get_password_hash(password)
        user = User(
            email=email,
            full_name="Admin User",
            hashed_password=password_hash,
            role=ROLE_SUPER_ADMIN,
            is_superuser=role_implies_superuser(ROLE_SUPER_ADMIN),
            # System-created: there is no inbox to prove control of, and an
            # unverified bootstrap admin is the one account a
            # require_email_verification deployment could strand.
            email_verified=True,
            # Password expiry keys off this column. Leaving it NULL made the
            # bootstrap admin indistinguishable from an account whose password
            # age is unknown, which is the one account that must never be locked
            # out by an expiry rule.
            password_changed_at=datetime.now(UTC),
            # NEVER pending, whatever require_account_approval says. This is the
            # break-glass account, and only a signed-in administrator can clear
            # an approval queue — a deployment whose sole administrator is IN the
            # queue is a deployment nobody can get into. Written explicitly rather
            # than left to the column default so the guarantee is visible here.
            approval_status=APPROVAL_APPROVED,
            approved_at=datetime.now(UTC),
            # A GENERATED password is a temporary authenticator: nobody chose it and
            # it is written to the container log at CRITICAL below, so it must not
            # stay this account's password (FedRAMP IA-5(1), "change default/temporary
            # authenticators immediately"). PUT /api/users/me is exempt from the
            # forced-change gate, so the hold has an exit.
            #
            # NOT applied to the other two cases, and the distinction is the point:
            # INITIAL_ADMIN_PASSWORD was chosen by the operator, so it is not a
            # temporary authenticator and holding them would break scripted hardened
            # provisioning that signs in with the credential it just configured. The
            # relaxed-environment dev credential must stay a plain login or every e2e
            # run and every local session starts behind a forced-change screen.
            must_change_password=generated,
        )
        db.add(user)
        # flush() to get the id: the history row needs it, and without one the
        # bootstrap super_admin — the highest-privilege account in the deployment —
        # was the ONE account whose seeded password was invisible to the reuse check.
        # "Rotating" it to the very same value returned zero history rows, was
        # accepted, and was audited as a successful password change, while the
        # credential in the logs stayed live. Every other creation path
        # (registration.py, users.create_user, invitations.py) already did this.
        db.flush()
        add_password_to_history(db, int(user.id), password_hash)
        db.commit()
        if generated:
            # The only time this password is ever visible. Logged at CRITICAL so it
            # survives a production log level and the operator cannot miss it.
            logger.critical(
                "Created bootstrap admin %s with a GENERATED password: %s\n"
                "Store it now — it is not recoverable. This account is flagged for a "
                "forced password change at first sign-in, and this value is recorded in "
                "its password history, so it cannot be re-set as the new password. "
                "Set INITIAL_ADMIN_PASSWORD to choose your own instead.",
                email,
                password,
            )
        else:
            logger.info("Created bootstrap admin user: %s (super_admin)", email)
    else:
        logger.debug("Admin user already exists")


def _heal_superuser_mirror(db: Session) -> None:
    """Repair any row where ``is_superuser`` disagrees with ``role``.

    ``is_superuser`` is a derived mirror of ``role == super_admin`` (see
    ``app.auth.roles``). Deployments seeded before ``role`` existed carry the legacy
    shape ``role='admin' + is_superuser=True``, which cannot reach the
    super_admin-gated surfaces (Settings → Authentication). The repair is keyed on
    the *shape*, not on the bootstrap email — an operator whose admin account uses
    a different address was previously left stuck.

    The role is promoted to match the flag rather than the flag cleared: the flag is
    the privilege those deployments actually granted. Idempotent, and never creates
    an account — only the derived mirror is repaired.
    """
    legacy_superusers = (
        db.query(User).filter(User.is_superuser.is_(True), User.role != ROLE_SUPER_ADMIN).all()
    )
    if not legacy_superusers:
        return

    for user in legacy_superusers:
        logger.info(
            "Promoting legacy superuser %s (role=%s) to %s",
            user.email,
            user.role,
            ROLE_SUPER_ADMIN,
        )
        user.role = ROLE_SUPER_ADMIN
        user.is_superuser = role_implies_superuser(ROLE_SUPER_ADMIN)
    db.commit()


def _admin_exists(db: Session) -> bool:
    """Whether any super_admin account already exists."""
    return (
        db.query(User).filter(User.role == ROLE_SUPER_ADMIN).first() is not None
        or db.query(User).filter(User.is_superuser.is_(True)).first() is not None
    )


def _ensure_default_tags(db: Session) -> None:
    """Create the system tag vocabulary if it doesn't exist.

    These are *system* tags: ``user_id IS NULL`` makes them visible to every
    account, which is the one case where an ownerless tag is intentional — so
    this is the one seeding path that deliberately does **not** go through
    ``resolve_or_create_tag`` (which always attributes an owner). The lookup
    must carry the same predicate — a user's own "Meeting" must not satisfy the
    seeder and leave the shared row missing.

    The rows are seeded **with** ``normalized_name``. Created without it they
    were invisible to normalized-exact resolution, so a user typing "interview"
    got a second tag alongside the seeded "Interview" — the four most common
    tags in every install, each able to fork a duplicate.
    """
    default_tags = ["Important", "Meeting", "Interview", "Personal"]

    for tag_name in default_tags:
        tag = db.query(Tag).filter(Tag.name == tag_name, Tag.user_id.is_(None)).first()
        if not tag:
            try:
                tag = Tag(
                    name=tag_name,
                    user_id=None,
                    normalized_name=normalize_tag_name(tag_name),
                )
                db.add(tag)
                db.flush()
                logger.info(f"Created default tag: {tag_name}")
            except IntegrityError:
                db.rollback()
                logger.debug(f"Default tag '{tag_name}' already exists (concurrent creation)")
        elif not tag.normalized_name:
            # Seeded before this column was maintained: repair in place, or the
            # shared row stays invisible to normalized-exact resolution forever.
            tag.normalized_name = normalize_tag_name(tag_name)

    db.commit()


def _ensure_system_prompts(db: Session) -> None:
    """Create system default prompts if they don't exist."""
    from app.core.default_prompts import SPEAKER_IDENTIFICATION_DESCRIPTION
    from app.core.default_prompts import SPEAKER_IDENTIFICATION_NAME
    from app.core.default_prompts import SPEAKER_IDENTIFICATION_PROMPT
    from app.core.default_prompts import UNIVERSAL_CONTENT_ANALYZER_DESCRIPTION
    from app.core.default_prompts import UNIVERSAL_CONTENT_ANALYZER_NAME
    from app.core.default_prompts import UNIVERSAL_CONTENT_ANALYZER_PROMPT

    prompts = [
        {
            "name": UNIVERSAL_CONTENT_ANALYZER_NAME,
            "description": UNIVERSAL_CONTENT_ANALYZER_DESCRIPTION,
            "prompt_text": UNIVERSAL_CONTENT_ANALYZER_PROMPT,
            "content_type": "general",
        },
        {
            "name": SPEAKER_IDENTIFICATION_NAME,
            "description": SPEAKER_IDENTIFICATION_DESCRIPTION,
            "prompt_text": SPEAKER_IDENTIFICATION_PROMPT,
            "content_type": "speaker_identification",
        },
    ]

    for prompt_data in prompts:
        existing = (
            db.query(SummaryPrompt)
            .filter(
                SummaryPrompt.is_system_default.is_(True),
                SummaryPrompt.content_type == prompt_data["content_type"],
            )
            .first()
        )
        if not existing:
            try:
                prompt = SummaryPrompt(
                    name=prompt_data["name"],
                    description=prompt_data["description"],
                    prompt_text=prompt_data["prompt_text"],
                    is_system_default=True,
                    content_type=prompt_data["content_type"],
                    is_active=True,
                )
                db.add(prompt)
                db.flush()
                logger.info(f"Created system prompt: {prompt_data['name']}")
            except IntegrityError:
                db.rollback()
                logger.debug(
                    f"System prompt '{prompt_data['name']}' already exists (concurrent creation)"
                )

    db.commit()


def init_db(db: Session) -> None:
    """Initialize database with seed data.

    Idempotent — safe to call on every startup.
    Creates admin user, default tags, and system prompts if missing.
    """
    # Repair legacy superuser rows before the bootstrap check, so it sees roles that
    # already agree with their is_superuser mirror.
    _heal_superuser_mirror(db)
    _ensure_admin_user(db)
    _ensure_default_tags(db)
    _ensure_system_prompts(db)


def main() -> None:
    """Run the init DB function (standalone entrypoint)."""
    logger.info("Creating initial data")
    db = next(get_db())
    init_db(db)
    logger.info("Initial data created")


if __name__ == "__main__":
    main()
