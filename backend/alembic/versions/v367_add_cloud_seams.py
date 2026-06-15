"""Add cloud-edition seams: organizations, usage events, external identity columns.

Open-core multitenancy seams. Community/self-hosted deployments are unaffected:
every column is nullable, every new table starts empty. Any commercial layer
that wants per-tenant billing/quota adds its own tables on top of these generic
ones. All DDL is idempotent (IF NOT EXISTS) so it is safe to re-run on
partially-migrated DBs.

Creates:
  - organization            : tenant mirror (external org id)
  - organization_membership : org membership mirror for fast joins
  - usage_event             : usage + product-analytics event spine

Alters:
  - "user"      : external_id (unique partial index), external_org_id (last-seen org)
  - media_file / collection / speaker / speaker_profile / speaker_collection /
    summary_prompt / custom_vocabulary / user_setting : nullable organization_id

Revision ID: v367_add_cloud_seams
Revises: v366_add_watch_sources
"""

from alembic import op

revision = "v367_add_cloud_seams"
down_revision = "v366_add_watch_sources"
branch_labels = None
depends_on = None

# Tables that gain a nullable organization_id scoping column. Pattern: every
# user_id-scoped resource that should be shareable within a company/tenant.
ORG_SCOPED_TABLES = (
    "media_file",
    "collection",
    "speaker",
    "speaker_profile",
    "speaker_collection",
    "summary_prompt",
    "custom_vocabulary",
    "user_setting",
)


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS organization (
            id SERIAL PRIMARY KEY,
            uuid UUID NOT NULL,
            external_org_id VARCHAR(255),
            name VARCHAR(255) NOT NULL,
            slug VARCHAR(255),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_organization_external_org_id "
        "ON organization (external_org_id)"
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_organization_uuid ON organization (uuid)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS organization_membership (
            id SERIAL PRIMARY KEY,
            organization_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
            role VARCHAR(20) NOT NULL DEFAULT 'org:member',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_org_membership UNIQUE (organization_id, user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_org_membership_user_id ON organization_membership (user_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_event (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
            organization_id INTEGER REFERENCES organization(id) ON DELETE SET NULL,
            event_type VARCHAR(50) NOT NULL,
            quantity NUMERIC(12,3) NOT NULL DEFAULT 1,
            unit VARCHAR(16),
            file_id INTEGER REFERENCES media_file(id) ON DELETE SET NULL,
            idempotency_key VARCHAR(128),
            event_metadata JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Partial unique index: idempotency is enforced only when a key is supplied
    # (Celery retries / webhook replays), free-form events can omit it.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_event_idempotency_key "
        "ON usage_event (idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_event_org_type_time "
        "ON usage_event (organization_id, event_type, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_event_user_time ON usage_event (user_id, created_at)"
    )

    # Normalize the user.auth_type CHECK constraint to the core auth types.
    op.execute('ALTER TABLE "user" DROP CONSTRAINT IF EXISTS users_auth_type_check')
    op.execute(
        'ALTER TABLE "user" ADD CONSTRAINT users_auth_type_check '
        "CHECK (auth_type IN ('local', 'ldap', 'keycloak', 'pki'))"
    )

    # External-IdP identity columns on user (mirrors keycloak_id pattern).
    op.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS external_id VARCHAR(255)')
    op.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS external_org_id VARCHAR(255)')
    op.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_external_id ON "user" (external_id) '
        "WHERE external_id IS NOT NULL"
    )

    # Nullable org-scoping column on every shareable resource (NULL = personal).
    for table in ORG_SCOPED_TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
            f"organization_id INTEGER REFERENCES organization(id)"
        )
        op.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_organization_id ON {table} (organization_id)"
        )


def downgrade():
    for table in ORG_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS organization_id")
    op.execute("DROP INDEX IF EXISTS uq_user_external_id")
    op.execute('ALTER TABLE "user" DROP COLUMN IF EXISTS external_org_id')
    op.execute('ALTER TABLE "user" DROP COLUMN IF EXISTS external_id')
    op.execute("DROP TABLE IF EXISTS usage_event")
    op.execute("DROP TABLE IF EXISTS organization_membership")
    op.execute("DROP TABLE IF EXISTS organization")
