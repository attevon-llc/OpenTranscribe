#!/usr/bin/env python
"""
Direct database inspection script to check tag tables
"""

import logging

from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.media import MediaFile
from app.models.tag import FileTag
from app.models.tag import Tag

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create direct connection to database
DATABASE_URL = settings.DATABASE_URL
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def inspect_database():
    """Inspect tag tables in the database"""
    db = SessionLocal()
    try:
        logger.info("Connecting to database...")
        # Test connection
        db.execute(text("SELECT 1"))
        logger.info("Database connection successful")

        # Query tags
        tags = db.query(Tag).all()
        logger.info("\n=== TAGS ===")
        for tag in tags:
            logger.info(f"ID: {tag.id}, Name: {tag.name}")

        # Query file tags
        file_tags = db.query(FileTag).all()
        logger.info("\n=== FILE TAGS ===")
        for ft in file_tags:
            logger.info(f"FileTag ID: {ft.id}, File ID: {ft.media_file_id}, Tag ID: {ft.tag_id}")

        # Get more details about file tags with join
        logger.info("\n=== DETAILED FILE TAGS ===")
        try:
            detailed = (
                db.query(FileTag, Tag.name, MediaFile.filename)
                .join(Tag, FileTag.tag_id == Tag.id)
                .join(MediaFile, FileTag.media_file_id == MediaFile.id)
                .all()
            )

            for ft, tag_name, filename in detailed:
                logger.info(f"FileTag ID: {ft.id}, File: {filename}, Tag: {tag_name}")
        except Exception as e:
            logger.error(f"Error querying detailed file tags: {e}")

        # Check for any tags with NULL name
        logger.info("\n=== CHECKING FOR INVALID TAGS ===")
        invalid_tags = db.query(Tag).filter(Tag.name.is_(None)).all()
        if invalid_tags:
            logger.warning(f"Found {len(invalid_tags)} tags with NULL name!")
            for tag in invalid_tags:
                logger.warning(f"Invalid tag ID: {tag.id}, Name: {tag.name}")
        else:
            logger.info("No invalid tags found with NULL name")

    except Exception as e:
        logger.error(f"Error inspecting database: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    inspect_database()
