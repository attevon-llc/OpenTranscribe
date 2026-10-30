#!/usr/bin/env python
"""
Database query script to check tag tables for debugging
"""

import logging
import os
import sys

# Add the parent directory to sys.path BEFORE any app imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.db.base import SessionLocal
from app.models.media import MediaFile
from app.models.tag import FileTag
from app.models.tag import Tag

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def query_tags():
    """Query all tags and file_tags from the database"""
    db = SessionLocal()
    try:
        logger.info("Connecting to database...")
        # Test connection
        db.execute(text("SELECT 1"))
        logger.info("Database connection successful")

        # Query tags
        tags = db.query(Tag).all()
        print("\n=== TAGS ===")
        for tag in tags:
            print(f"ID: {tag.id}, Name: {tag.name}")

        # Query file tags
        file_tags = db.query(FileTag).all()
        print("\n=== FILE TAGS ===")
        for ft in file_tags:
            print(f"FileTag ID: {ft.id}, File ID: {ft.media_file_id}, Tag ID: {ft.tag_id}")

        # Get more details about file tags with join
        print("\n=== DETAILED FILE TAGS ===")
        detailed = (
            db.query(FileTag, Tag.name, MediaFile.filename)
            .join(Tag, FileTag.tag_id == Tag.id)
            .join(MediaFile, FileTag.media_file_id == MediaFile.id)
            .all()
        )

        for ft, tag_name, filename in detailed:
            print(f"FileTag ID: {ft.id}, File: {filename}, Tag: {tag_name}")

    except Exception as e:
        print(f"Error querying database: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    query_tags()
