"""Defensive guard: ensure every ``uuid`` identifier column is native ``uuid`` type.

Current and recent deployments already store every ``uuid`` identifier column as
native PostgreSQL ``uuid`` (the models have used ``UUID(as_uuid=True)`` for a long
time). On those databases this migration is a verified **no-op**.

It exists only to protect *very old* deployments that might still carry a
``character varying(36)`` ``uuid`` column from a pre-native-uuid schema, so the
adoption of UUIDv7 generation (which mints real ``uuid.UUID`` values) does not break
their inserts. Per table that has a ``uuid`` column whose ``data_type`` is NOT
already ``uuid``, it runs::

    ALTER TABLE <t> ALTER COLUMN uuid TYPE uuid USING uuid::uuid

The conversion is gated on ``information_schema.columns.data_type`` so it touches a
column only when it is still ``character varying`` (or any non-uuid type holding
valid uuid strings). Idempotent and safe to re-run: once a column is ``uuid`` the
per-table block does nothing.

This does NOT touch token-shaped ``varchar(36)`` columns that are not uuid
identifiers (``refresh_token.jti``, ``refresh_token.token_hash``); they are never
selected because the guard targets only columns literally named ``uuid``.

Revision ID: v368_uuid_native_type_guard
Revises: v367_add_cloud_seams
"""

from alembic import op

revision = "v368_uuid_native_type_guard"
down_revision = "v367_add_cloud_seams"
branch_labels = None
depends_on = None


def upgrade():
    # Discover every base table that has a column literally named "uuid" whose
    # type is not yet native ``uuid``, and convert it in place. A single DO block
    # keeps this self-contained, idempotent, and a true no-op on native schemas
    # (the inner SELECT returns zero rows when all uuid columns are already uuid).
    op.execute(
        """
        DO $$
        DECLARE
            r RECORD;
        BEGIN
            FOR r IN
                SELECT table_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND column_name = 'uuid'
                  AND data_type <> 'uuid'
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I ALTER COLUMN uuid TYPE uuid USING uuid::uuid',
                    r.table_name
                );
            END LOOP;
        END
        $$;
        """
    )


def downgrade():
    # Intentionally a no-op. Converting native ``uuid`` columns back to
    # ``varchar(36)`` would be a regression with no legitimate use case, and we
    # never want a downgrade to widen the type and reintroduce the old failure
    # mode. The forward migration is non-destructive (value-preserving cast).
    pass
