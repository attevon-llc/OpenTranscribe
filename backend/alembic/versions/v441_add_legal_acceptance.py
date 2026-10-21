"""Record an administrator's acceptance of a versioned legal document.

Why
---
The app now serves versioned legal documents (``app.legal``). An acceptance stores the
document key, its version and the SHA-256 of the exact bytes the user saw, so a changed
text is a new acceptance rather than a silent re-use of an old one. The UNIQUE key makes
re-accepting the same text a no-op. ``ON DELETE CASCADE``: deleting an account deletes its
acceptances (GDPR Art. 17), the same as every other per-user table.

Affects every deployment; empty until an administrator accepts a document.

Revision ID: v441_add_legal_acceptance
Revises: v440_add_password_last_self_changed_at
Create Date: 2026-10-21
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v441_add_legal_acceptance"
down_revision = "v440_add_password_last_self_changed_at"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = """
    CREATE TABLE IF NOT EXISTS legal_acceptance (
        id SERIAL PRIMARY KEY,
        uuid UUID NOT NULL,
        user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        document_key VARCHAR(128) NOT NULL,
        version VARCHAR(32) NOT NULL,
        sha256 CHAR(64) NOT NULL,
        accepted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        ip_address VARCHAR(64),
        CONSTRAINT uq_legal_acceptance_user_doc_sha UNIQUE (user_id, document_key, sha256)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ix_legal_acceptance_uuid ON legal_acceptance (uuid);
    CREATE INDEX IF NOT EXISTS ix_legal_acceptance_id ON legal_acceptance (id);
    CREATE INDEX IF NOT EXISTS ix_legal_acceptance_user_id ON legal_acceptance (user_id);
"""

DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS legal_acceptance;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
