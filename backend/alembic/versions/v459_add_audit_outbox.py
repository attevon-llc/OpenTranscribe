"""Transactional outbox for governance audit events (issue #1329).

Why
---
Governance audit events (a transcript edit on a held file, placing or releasing a hold, a
collection's visibility or membership changing) were written to the audit sink (a log line plus
the OpenSearch ``audit-logs-*`` index) AFTER the change had committed, best effort. When that
write failed the change stood and the event was gone, with only a server log line left. For a
legal hold that is the record the hold exists to keep.

``audit_outbox`` is the documented Transactional Outbox pattern: the request inserts the event
row in the SAME database transaction as the change, so the two commit or roll back together, and
a relay (``app.services.audit_outbox``, run after the commit and by the ``audit.outbox_relay``
beat task) copies pending rows to the sink, marks them ``sent``, and retries with backoff while
the sink is down. ``event_uuid`` is the sink document's id, so a resend after a crash never
indexes a second copy.

Columns: ``payload`` is the exact event the sink receives (ids, kinds, counts; never transcript
text, names or hold reasons, as for every audit event). ``status`` is ``pending``, ``sent`` or
``rejected`` (the sink refused the document as malformed, so a retry cannot succeed; the row is
the record and the stuck-event gauge counts it). ``next_attempt_at`` doubles as the claim lease.
No foreign keys: the row must outlive the user, file or organization it names until it is sent.
``sent`` rows are deleted by the relay after a short retention (the sink holds them).

Every deployment gets the table; it stays empty where nothing governance-shaped happens. The
detection arm keys on ``ck_audit_outbox_status``, created last. The downgrade drops the table: any
row still ``pending`` is an event the sink never received, so drain the relay first.

Revision ID: v459_add_audit_outbox
Revises: v458_speaker_machine_identity_index
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v459_add_audit_outbox"
down_revision = "v458_speaker_machine_identity_index"
branch_labels = None
depends_on = None

#: The values ``audit_outbox.status`` may take, as spelled in ``ck_audit_outbox_status`` below
#: (``app.models.audit_outbox`` mirrors them; the consistency test asserts all three agree).
STATUSES = ("pending", "sent", "rejected")

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS audit_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_uuid UUID NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_outbox_event_uuid ON audit_outbox (event_uuid);
CREATE INDEX IF NOT EXISTS ix_audit_outbox_due
    ON audit_outbox (next_attempt_at, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_audit_outbox_sent_at
    ON audit_outbox (sent_at) WHERE status = 'sent';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_audit_outbox_sent_at') THEN
        ALTER TABLE audit_outbox ADD CONSTRAINT ck_audit_outbox_sent_at
            CHECK ((status = 'sent') = (sent_at IS NOT NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_audit_outbox_status') THEN
        ALTER TABLE audit_outbox ADD CONSTRAINT ck_audit_outbox_status
            CHECK (status IN ('pending', 'sent', 'rejected'));
    END IF;
END $$;
"""

DOWNGRADE_SQL = "DROP TABLE IF EXISTS audit_outbox;"


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
