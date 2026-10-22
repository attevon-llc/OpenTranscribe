"""Log who was told about a takedown event (issue #1254).

Why
---
A takedown notice record says what was decided; it does not say who was told. The
in-app owner notice is a fire-and-forget publish, and the optional email has a transport
that may not exist. ``takedown_notification`` records each attempt per link, event,
recipient and channel, so "was the owner notified" has an answer that outlives the log.

What
----
One row per (link, event, recipient, channel), unique so a retried notify call is
idempotent. Every textual column is a CHECK-bounded enum: no address, no error text, no
message body. Failure detail stays in the application log.

Downgrade drops the table and the delivery history with it.

Revision ID: v444_add_takedown_notification
Revises: v443_add_takedown_notice_tables
Create Date: 2026-10-21
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v444_add_takedown_notification"
down_revision = "v443_add_takedown_notice_tables"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = """
    CREATE TABLE IF NOT EXISTS takedown_notification (
        id SERIAL PRIMARY KEY,
        link_id INTEGER NOT NULL REFERENCES takedown_notice_file(id) ON DELETE CASCADE,
        event VARCHAR(16) NOT NULL,
        recipient_kind VARCHAR(16) NOT NULL,
        recipient_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
        recipient_user_uuid UUID NOT NULL,
        channel VARCHAR(16) NOT NULL,
        status VARCHAR(32) NOT NULL,
        attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS ix_takedown_notification_link_id
        ON takedown_notification (link_id);

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_takedown_notification_delivery'
        ) THEN
            ALTER TABLE takedown_notification ADD CONSTRAINT uq_takedown_notification_delivery
                UNIQUE (link_id, event, recipient_user_uuid, channel);
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_takedown_notification_event'
        ) THEN
            ALTER TABLE takedown_notification ADD CONSTRAINT ck_takedown_notification_event
                CHECK (event IN ('quarantined', 'released'));
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_takedown_notification_recipient_kind'
        ) THEN
            ALTER TABLE takedown_notification
                ADD CONSTRAINT ck_takedown_notification_recipient_kind
                CHECK (recipient_kind IN ('owner', 'org_admin'));
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_takedown_notification_channel'
        ) THEN
            ALTER TABLE takedown_notification ADD CONSTRAINT ck_takedown_notification_channel
                CHECK (channel IN ('in_app', 'email'));
        END IF;
        -- Created last: the detection arm keys on it.
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_takedown_notification_status'
        ) THEN
            ALTER TABLE takedown_notification ADD CONSTRAINT ck_takedown_notification_status
                CHECK (status IN ('sent', 'failed', 'skipped_no_transport',
                                  'skipped_no_recipient'));
        END IF;
    END $$;
"""

DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS takedown_notification;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
