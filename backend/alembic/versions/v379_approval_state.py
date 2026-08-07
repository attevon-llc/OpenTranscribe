"""Administrator approval state for newly provisioned accounts.

Adds three columns to ``user``:

``approval_status``  ``'pending' | 'approved' | 'rejected'``, NOT NULL, default
    ``'approved'``
    The default is the whole upgrade story. Every existing row — and every account
    created by any path that does not explicitly opt in — is ``approved``, so a
    deployment that takes this update and never touches the new
    ``require_account_approval`` setting behaves exactly as it did. Only
    ``app/auth/approval.initial_approval_status`` ever writes ``'pending'``, and only
    while that setting is on.

    Deliberately NOT folded into ``is_active``. Deactivation revokes an account that
    was once usable; approval gates one that never has been. Sharing a column would
    make "approve" and "re-enable" the same write with different meanings, and would
    lose the distinction the audit trail needs.

``approved_at`` / ``approved_by``
    Who decided, and when. ``approved_by`` is a self-referential FK to ``user.id``
    with ``ON DELETE SET NULL``: deleting the administrator who approved an account
    must not cascade into deleting the account. No ORM relationship is declared
    against it — the value is read for display only, and a second relationship pair
    on ``user`` would need an explicit ``foreign_keys=`` on both sides (the trap
    documented in ``app/models/CLAUDE.md``).

``ck_user_approval_status_valid``
    The closed value set, matching ``app.auth.approval.VALID_APPROVAL_STATUSES``. An
    unrecognised value here would be read as "not pending, not rejected" by
    ``approval.is_pending``/``is_rejected`` — i.e. it would fail OPEN — so the
    constraint is what makes those helpers' fail-safe reads sound. Rows are
    normalised to ``'approved'`` before it is added so the ALTER cannot fail on a
    hand-edited database.

``ix_user_approval_status``
    The pending queue is ``WHERE approval_status = 'pending'`` on a table that is
    almost entirely ``'approved'``; without an index the admin list is a seq scan of
    every account.

COMMUNITY EDITION: no behaviour change on upgrade. The feature is off by default
(``require_account_approval``), and with it off nothing is ever written ``'pending'``.

Revision ID: v379_approval_state
Revises: v378_oidc_identity_columns
Create Date: 2026-08-07
"""

from alembic import op

revision = "v379_approval_state"
down_revision = "v378_oidc_identity_columns"
branch_labels = None
depends_on = None

#: The states the database accepts. Kept as one module-level constant so the CHECK,
#: the normalising UPDATE and the consistency test cannot drift from each other, and
#: so the test can assert ``app.auth.approval.VALID_APPROVAL_STATUSES`` matches it.
VALID_APPROVAL_STATUSES_SQL = "'pending', 'approved', 'rejected'"

#: Additive and idempotent throughout: the startup runner stamps untracked databases
#: by fingerprint, so a revision routinely re-runs against a schema that already
#: carries part of its changes.
UPGRADE_SQL = """
    ALTER TABLE "user"
        ADD COLUMN IF NOT EXISTS approval_status VARCHAR(20) NOT NULL DEFAULT 'approved';
    ALTER TABLE "user" ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP WITH TIME ZONE;
    ALTER TABLE "user" ADD COLUMN IF NOT EXISTS approved_by INTEGER;

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'fk_user_approved_by'
        ) THEN
            ALTER TABLE "user" ADD CONSTRAINT fk_user_approved_by
                FOREIGN KEY (approved_by) REFERENCES "user" (id) ON DELETE SET NULL;
        END IF;
    END $$;
"""

#: Split from the DDL so the normalisation runs against columns that certainly
#: exist, including on a re-run where the ADD COLUMNs were no-ops.
_CONSTRAINT_TEMPLATE = """
    UPDATE "user" SET approval_status = 'approved'
     WHERE approval_status IS NULL OR approval_status NOT IN ({statuses});

    ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_approval_status_valid;
    ALTER TABLE "user" ADD CONSTRAINT ck_user_approval_status_valid
        CHECK (approval_status IN ({statuses}));

    CREATE INDEX IF NOT EXISTS ix_user_approval_status ON "user" (approval_status);
"""

CONSTRAINT_SQL = _CONSTRAINT_TEMPLATE.format(statuses=VALID_APPROVAL_STATUSES_SQL)  # nosec B608

#: Mirror image. Dropping the columns takes the CHECK and the FK with them, but the
#: index is named separately and is dropped explicitly for symmetry.
DOWNGRADE_SQL = """
    DROP INDEX IF EXISTS ix_user_approval_status;
    ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_approval_status_valid;
    ALTER TABLE "user" DROP CONSTRAINT IF EXISTS fk_user_approved_by;
    ALTER TABLE "user" DROP COLUMN IF EXISTS approved_by;
    ALTER TABLE "user" DROP COLUMN IF EXISTS approved_at;
    ALTER TABLE "user" DROP COLUMN IF EXISTS approval_status;
"""


def upgrade():
    op.execute(UPGRADE_SQL)
    op.execute(CONSTRAINT_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
