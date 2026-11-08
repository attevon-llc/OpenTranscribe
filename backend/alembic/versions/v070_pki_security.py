"""v0.7.0 - Add PKI security enhancements

Revision ID: v070_add_pki_security_enhancements
Revises: v060_add_transcript_overlap
Create Date: 2026-02-04

Security enhancements for PKI/X.509 certificate authentication:
    - Add all PKI certificate metadata columns
    - Index on pki_fingerprint_sha256 for fast certificate lookup
    - Index on pki_issuer_dn for issuer-based queries
    - Unique constraint on (pki_serial_number, pki_issuer_dn) to prevent duplicate certs

These changes support the FedRAMP security fixes including:
    - Certificate expiration validation
    - Certificate uniqueness enforcement
    - Efficient certificate-based user lookup
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v070_pki_security"
down_revision = "v060_add_transcript_overlap"
branch_labels = None
depends_on = None


def upgrade():
    """Add PKI security enhancement columns and indexes."""
    # Expand token_hash column from 64 to 128 chars for SHA-512 (FIPS 140-3)
    op.execute(
        """
        ALTER TABLE refresh_token
        ALTER COLUMN token_hash TYPE VARCHAR(128);
        """
    )

    # Add all missing PKI columns
    pki_columns = [
        ("pki_serial_number", "VARCHAR(128)"),
        ("pki_issuer_dn", "VARCHAR(512)"),
        ("pki_organization", "VARCHAR(256)"),
        ("pki_organizational_unit", "VARCHAR(256)"),
        ("pki_common_name", "VARCHAR(256)"),
        ("pki_not_before", "TIMESTAMP WITH TIME ZONE"),
        ("pki_not_after", "TIMESTAMP WITH TIME ZONE"),
        ("pki_fingerprint_sha256", "VARCHAR(64)"),
    ]

    for col_name, col_type in pki_columns:
        sql = f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'user' AND column_name = '{col_name}'
                ) THEN
                    ALTER TABLE "user"
                    ADD COLUMN {col_name} {col_type};
                END IF;
            END $$;
            """
        op.execute(sql)

    # Add index on pki_fingerprint_sha256 for fast certificate lookup
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'idx_user_pki_fingerprint_sha256'
            ) THEN
                CREATE INDEX idx_user_pki_fingerprint_sha256
                ON "user"(pki_fingerprint_sha256)
                WHERE pki_fingerprint_sha256 IS NOT NULL;
            END IF;
        END $$;
        """
    )

    # Add index on pki_issuer_dn for issuer-based queries
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'idx_user_pki_issuer_dn'
            ) THEN
                CREATE INDEX idx_user_pki_issuer_dn
                ON "user"(pki_issuer_dn)
                WHERE pki_issuer_dn IS NOT NULL;
            END IF;
        END $$;
        """
    )

    # Add unique constraint on (pki_serial_number, pki_issuer_dn)
    # This prevents duplicate certificate registrations
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'user_pki_cert_unique'
            ) THEN
                ALTER TABLE "user"
                ADD CONSTRAINT user_pki_cert_unique
                UNIQUE (pki_serial_number, pki_issuer_dn)
                DEFERRABLE INITIALLY DEFERRED;
            END IF;
        END $$;
        """
    )


def downgrade():
    """Remove PKI security enhancement columns and indexes."""
    op.execute('ALTER TABLE "user" DROP CONSTRAINT IF EXISTS user_pki_cert_unique')
    op.execute("DROP INDEX IF EXISTS idx_user_pki_issuer_dn")
    op.execute("DROP INDEX IF EXISTS idx_user_pki_fingerprint_sha256")

    pki_columns = [
        "pki_fingerprint_sha256",
        "pki_not_after",
        "pki_not_before",
        "pki_common_name",
        "pki_organizational_unit",
        "pki_organization",
        "pki_issuer_dn",
        "pki_serial_number",
    ]

    for col_name in pki_columns:
        op.execute(f'ALTER TABLE "user" DROP COLUMN IF EXISTS {col_name};')
