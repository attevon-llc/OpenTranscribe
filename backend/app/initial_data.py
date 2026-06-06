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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.db.base import get_db
from app.models.media import Tag
from app.models.prompt import SummaryPrompt
from app.models.user import User

# No module-level basicConfig: this module is imported during app startup and
# a default root handler here would double every log line. configure_logging()
# (app.core.logging_config) owns the root handler.
logger = logging.getLogger(__name__)


def _ensure_admin_user(db: Session) -> None:
    """Create the default admin user if it doesn't exist."""
    user = db.query(User).filter(User.email == "admin@example.com").first()
    if not user:
        user = User(
            email="admin@example.com",
            full_name="Admin User",
            hashed_password=get_password_hash("password"),
            is_superuser=True,
            role="admin",
        )
        db.add(user)
        db.commit()
        logger.info("Created default admin user: admin@example.com")
    else:
        logger.debug("Admin user already exists")


def _ensure_default_tags(db: Session) -> None:
    """Create default tags if they don't exist."""
    default_tags = ["Important", "Meeting", "Interview", "Personal"]

    for tag_name in default_tags:
        tag = db.query(Tag).filter(Tag.name == tag_name).first()
        if not tag:
            try:
                tag = Tag(name=tag_name)
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
