"""Add Watch Sources auto-import tables (issue #26).

Creates the four tables backing the watch-source feature:
  - watch_source            : unified local/s3/smb source config (encrypted creds)
  - watch_source_file       : per-file tracking (dedup fingerprint, status, links)
  - email_notification_config : reusable SMTP/M365/Exchange config (encrypted creds)
  - watch_source_email      : junction linking sources to email configs

No media_file ALTER — the imohash column already shipped in v361. All DDL is
idempotent (IF NOT EXISTS) so it is safe to re-run on partially-migrated DBs.

Revision ID: v366_add_watch_sources
Revises: v365_add_prompt_shared_by
"""

from alembic import op

revision = "v366_add_watch_sources"
down_revision = "v365_add_prompt_shared_by"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS watch_source (
            id SERIAL PRIMARY KEY,
            uuid UUID NOT NULL,
            name VARCHAR(200) NOT NULL,
            source_type VARCHAR(20) NOT NULL,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            local_path VARCHAR(2000),
            delete_after_import BOOLEAN NOT NULL DEFAULT FALSE,
            s3_endpoint_url VARCHAR(500),
            s3_bucket_name VARCHAR(255),
            s3_prefix VARCHAR(1000),
            s3_region VARCHAR(100),
            s3_access_key_id TEXT,
            encrypted_s3_secret_key TEXT,
            s3_use_ssl BOOLEAN NOT NULL DEFAULT TRUE,
            smb_server VARCHAR(255),
            smb_share VARCHAR(255),
            smb_path VARCHAR(2000) DEFAULT '/',
            smb_username VARCHAR(255),
            encrypted_smb_password TEXT,
            smb_domain VARCHAR(255),
            smb_port INTEGER NOT NULL DEFAULT 445,
            user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
            polling_interval_minutes INTEGER NOT NULL DEFAULT 15,
            use_fs_events BOOLEAN NOT NULL DEFAULT FALSE,
            file_extensions TEXT,
            skip_files_older_than_days INTEGER,
            recursive BOOLEAN NOT NULL DEFAULT TRUE,
            auto_transcribe BOOLEAN NOT NULL DEFAULT TRUE,
            min_speakers INTEGER DEFAULT 1,
            max_speakers INTEGER DEFAULT 20,
            collection_ids JSON,
            tag_names JSON,
            multipart_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            multipart_regex VARCHAR(500) NOT NULL DEFAULT '^(.+?)_P(\\d{3})(\\.[^.]+)$',
            multipart_time_window_hours INTEGER NOT NULL DEFAULT 24,
            multipart_wait_scans INTEGER NOT NULL DEFAULT 3,
            upload_stitched_to_source BOOLEAN NOT NULL DEFAULT FALSE,
            last_scan_at TIMESTAMPTZ,
            last_scan_status VARCHAR(20),
            last_scan_message TEXT,
            last_scan_files_found INTEGER NOT NULL DEFAULT 0,
            last_scan_files_imported INTEGER NOT NULL DEFAULT 0,
            last_scan_files_skipped INTEGER NOT NULL DEFAULT 0,
            last_scan_duration_seconds DOUBLE PRECISION,
            total_files_imported INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_watch_source_uuid ON watch_source(uuid)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_watch_source_user_id ON watch_source(user_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_watch_source_enabled "
        "ON watch_source(is_enabled) WHERE is_enabled = TRUE"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS watch_source_file (
            id SERIAL PRIMARY KEY,
            uuid UUID NOT NULL,
            watch_source_id INTEGER NOT NULL REFERENCES watch_source(id) ON DELETE CASCADE,
            remote_path VARCHAR(2000) NOT NULL,
            filename VARCHAR(500) NOT NULL,
            file_size BIGINT,
            file_modified_at TIMESTAMPTZ,
            imohash VARCHAR(64),
            media_file_id INTEGER REFERENCES media_file(id) ON DELETE SET NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'pending',
            skip_reason VARCHAR(50),
            part_group VARCHAR(500),
            part_number INTEGER,
            error_message TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            processed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_watch_source_file_uuid ON watch_source_file(uuid)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS _watch_source_file_path_unique "
        "ON watch_source_file(watch_source_id, remote_path)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_watch_source_file_source_imohash "
        "ON watch_source_file(watch_source_id, imohash)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_watch_source_file_imohash ON watch_source_file(imohash)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_watch_source_file_part_group "
        "ON watch_source_file(part_group, watch_source_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_watch_source_file_status ON watch_source_file(status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_watch_source_file_media_file_id "
        "ON watch_source_file(media_file_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS email_notification_config (
            id SERIAL PRIMARY KEY,
            uuid UUID NOT NULL,
            name VARCHAR(200) NOT NULL,
            provider VARCHAR(20) NOT NULL,
            smtp_host VARCHAR(255),
            smtp_port INTEGER,
            smtp_use_tls BOOLEAN NOT NULL DEFAULT TRUE,
            smtp_username VARCHAR(255),
            encrypted_smtp_password TEXT,
            m365_tenant_id VARCHAR(255),
            m365_client_id VARCHAR(255),
            encrypted_m365_client_secret TEXT,
            exchange_server VARCHAR(255),
            exchange_domain VARCHAR(255),
            exchange_username VARCHAR(255),
            encrypted_exchange_password TEXT,
            from_address VARCHAR(255),
            default_recipients TEXT,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            last_tested_at TIMESTAMPTZ,
            test_status VARCHAR(20),
            test_message TEXT,
            created_by INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_email_notification_config_uuid "
        "ON email_notification_config(uuid)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS watch_source_email (
            id SERIAL PRIMARY KEY,
            watch_source_id INTEGER NOT NULL REFERENCES watch_source(id) ON DELETE CASCADE,
            email_config_id INTEGER NOT NULL
                REFERENCES email_notification_config(id) ON DELETE CASCADE,
            additional_recipients TEXT,
            notify_on_success BOOLEAN NOT NULL DEFAULT TRUE,
            notify_on_error BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS _watch_source_email_unique "
        "ON watch_source_email(watch_source_id, email_config_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_watch_source_email_source "
        "ON watch_source_email(watch_source_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_watch_source_email_config "
        "ON watch_source_email(email_config_id)"
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS watch_source_email")
    op.execute("DROP TABLE IF EXISTS watch_source_file")
    op.execute("DROP TABLE IF EXISTS email_notification_config")
    op.execute("DROP TABLE IF EXISTS watch_source")
