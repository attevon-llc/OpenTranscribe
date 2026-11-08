"""v1.2.0 - Add remaining FK indexes for query performance

Revision ID: v120_add_remaining_fk_indexes
Revises: v110_add_missing_fk_indexes
Create Date: 2026-02-09

PostgreSQL does NOT auto-create indexes on FK columns. This migration adds
indexes on all remaining high-traffic FK columns identified by audit:

1. speaker(user_id) - every speaker list query
2. speaker(media_file_id) - speaker-file associations
3. speaker(profile_id) - profile-based cross-media queries
4. file_tag(media_file_id) - tag filtering
5. file_tag(tag_id) - tag association queries
6. task(user_id) - task queries
7. task(media_file_id) - task-file association
8. collection_member(collection_id) - collection member queries
9. collection_member(media_file_id) - media-collection queries
10. speaker_profile(user_id) - profile queries
11. transcript_segment(speaker_id) - speaker segment counts
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v120_add_remaining_fk_indexes"
down_revision = "v110_add_missing_fk_indexes"
branch_labels = None
depends_on = None

# (index_name, table, columns)
_INDEXES = [
    ("idx_speaker_user_id", "speaker", "user_id"),
    ("idx_speaker_media_file_id", "speaker", "media_file_id"),
    ("idx_speaker_profile_id", "speaker", "profile_id"),
    ("idx_file_tag_media_file_id", "file_tag", "media_file_id"),
    ("idx_file_tag_tag_id", "file_tag", "tag_id"),
    ("idx_task_user_id", "task", "user_id"),
    ("idx_task_media_file_id", "task", "media_file_id"),
    ("idx_collection_member_collection_id", "collection_member", "collection_id"),
    ("idx_collection_member_media_file_id", "collection_member", "media_file_id"),
    ("idx_speaker_profile_user_id", "speaker_profile", "user_id"),
    ("idx_transcript_segment_speaker_id", "transcript_segment", "speaker_id"),
]


def upgrade():
    """Add missing FK indexes."""
    for idx_name, table, columns in _INDEXES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE indexname = '{idx_name}'
                ) THEN
                    CREATE INDEX {idx_name} ON {table}({columns});
                END IF;
            END $$;
            """
        )


def downgrade():
    """Remove indexes. Safe — no data loss."""
    for idx_name, _, _ in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {idx_name};")
