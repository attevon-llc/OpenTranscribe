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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import role_implies_superuser
from app.core.security import get_password_hash
from app.db.base import get_db
from app.models.media import Tag
from app.models.prompt import SummaryPrompt
from app.models.user import User

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
    """
    email, password, generated = _resolve_bootstrap_admin()

    user = db.query(User).filter(User.email == email).first()

    # In a hardened environment, never resurrect the dev credential — and don't create
    # a second bootstrap admin if the operator already has one under another address.
    if not user and _admin_exists(db):
        logger.debug("An admin already exists; skipping bootstrap admin creation")
        return

    if not user:
        user = User(
            email=email,
            full_name="Admin User",
            hashed_password=get_password_hash(password),
            role=ROLE_SUPER_ADMIN,
            is_superuser=role_implies_superuser(ROLE_SUPER_ADMIN),
        )
        db.add(user)
        db.commit()
        if generated:
            # The only time this password is ever visible. Logged at CRITICAL so it
            # survives a production log level and the operator cannot miss it.
            logger.critical(
                "Created bootstrap admin %s with a GENERATED password: %s\n"
                "Store it now and change it after first login — it is not recoverable. "
                "Set INITIAL_ADMIN_PASSWORD to choose your own instead.",
                email,
                password,
            )
        else:
            logger.info("Created bootstrap admin user: %s (super_admin)", email)
    elif user.role != ROLE_SUPER_ADMIN and user.is_superuser:
        # Self-heal a legacy default admin (role='admin' + is_superuser=True),
        # which could not reach the super_admin-gated surfaces. Idempotent.
        user.role = ROLE_SUPER_ADMIN
        user.is_superuser = True
        db.commit()
        logger.info("Promoted legacy default admin %s to super_admin", email)
    else:
        logger.debug("Admin user already exists")


def _admin_exists(db: Session) -> bool:
    """Whether any super_admin account already exists."""
    return (
        db.query(User).filter(User.role == ROLE_SUPER_ADMIN).first() is not None
        or db.query(User).filter(User.is_superuser.is_(True)).first() is not None
    )


def _ensure_default_tags(db: Session) -> None:
    """Create the system tag vocabulary if it doesn't exist.

    These are *system* tags: ``user_id IS NULL`` makes them visible to every
    account, which is the one case where an ownerless tag is intentional. The
    lookup must carry the same predicate — a user's own "Meeting" must not
    satisfy the seeder and leave the shared row missing.
    """
    default_tags = ["Important", "Meeting", "Interview", "Personal"]

    for tag_name in default_tags:
        tag = db.query(Tag).filter(Tag.name == tag_name, Tag.user_id.is_(None)).first()
        if not tag:
            try:
                tag = Tag(name=tag_name, user_id=None)
                db.add(tag)
                db.flush()
                logger.info(f"Created default tag: {tag_name}")
            except IntegrityError:
                db.rollback()
                logger.debug(f"Default tag '{tag_name}' already exists (concurrent creation)")

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
