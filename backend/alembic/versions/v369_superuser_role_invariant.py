"""Unify privilege model: ``is_superuser`` mirrors ``role == 'super_admin'``.

``User.role`` is the single source of truth for authorization. ``is_superuser``
is a derived boolean that must equal ``role == 'super_admin'``. Historically the
two could diverge (the default admin was seeded as ``role='admin'`` +
``is_superuser=True``, so it could not reach the super_admin-gated surfaces such
as Authentication config).

This migration:
  1. Promotes the platform owner to ``super_admin`` (default ``admin@example.com``;
     if absent, the single oldest ``is_superuser`` account) so at least one
     super_admin exists and the default admin works out of the box.
  2. Reconciles every row so ``is_superuser = (role = 'super_admin')``.
  3. Adds a CHECK constraint enforcing the invariant going forward, plus a
     CHECK constraint restricting ``role`` to the known values.

All steps are idempotent and safe to re-run.

Revision ID: v369_superuser_role_invariant
Revises: v368_uuid_native_type_guard
"""

from alembic import op

revision = "v369_superuser_role_invariant"
down_revision = "v368_uuid_native_type_guard"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Ensure a platform super_admin exists.
    #    a) Promote the well-known default admin if present.
    op.execute(
        """
        UPDATE "user"
           SET role = 'super_admin'
         WHERE email = 'admin@example.com'
           AND role <> 'super_admin';
        """
    )
    #    b) If still no super_admin, promote the single oldest is_superuser account
    #       (legacy "superuser" admins). Picks exactly one to avoid broadly granting
    #       platform ownership.
    op.execute(
        """
        DO $$
        DECLARE
            target_id integer;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM "user" WHERE role = 'super_admin') THEN
                SELECT id INTO target_id
                  FROM "user"
                 WHERE is_superuser = true
                 ORDER BY id ASC
                 LIMIT 1;
                IF target_id IS NOT NULL THEN
                    UPDATE "user" SET role = 'super_admin' WHERE id = target_id;
                END IF;
            END IF;
        END $$;
        """
    )

    # 2. Reconcile the derived flag for every row.
    op.execute(
        """
        UPDATE "user"
           SET is_superuser = (role = 'super_admin')
         WHERE is_superuser <> (role = 'super_admin');
        """
    )

    # 3. Enforce the invariants. Guarded so the migration is idempotent.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_user_role_valid'
            ) THEN
                ALTER TABLE "user"
                    ADD CONSTRAINT ck_user_role_valid
                    CHECK (role IN ('user', 'admin', 'super_admin'));
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_user_superuser_matches_role'
            ) THEN
                ALTER TABLE "user"
                    ADD CONSTRAINT ck_user_superuser_matches_role
                    CHECK (is_superuser = (role = 'super_admin'));
            END IF;
        END $$;
        """
    )


def downgrade():
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_user_superuser_matches_role'
            ) THEN
                ALTER TABLE "user" DROP CONSTRAINT ck_user_superuser_matches_role;
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_user_role_valid'
            ) THEN
                ALTER TABLE "user" DROP CONSTRAINT ck_user_role_valid;
            END IF;
        END $$;
        """
    )
