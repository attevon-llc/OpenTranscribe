"""Rename the stored auth-config keys from the vendor prefix to ``oidc_``.

OpenTranscribe claims support for "any OpenID Connect provider", and since #353 that
is technically true — endpoints come from the provider's discovery document. But every
field an Authentik or Okta administrator typed into was still named for one specific
vendor, which is both misleading and, in the roles-claim case, actively broke
non-realm providers. The whole surface is renamed ``oidc_*``; this revision moves the
*data* so an existing deployment's configuration survives the rename instead of
silently reverting to defaults (which, for ``oidc_enabled``, means SSO switching
itself off on upgrade).

What moves
----------
``auth_config``
    ``config_key`` ``keycloak_<x>`` -> ``oidc_<x>``, and ``category`` -> ``'oidc'``.
    ``config_key`` is globally UNIQUE, so the UPDATE carries a ``NOT EXISTS`` guard:
    re-running must not raise, and a database that somehow holds both spellings keeps
    the new row. The DELETE that follows removes only the rows the UPDATE skipped for
    exactly that reason.

``auth_config_audit``
    Same key rename. Without it ``GET /api/auth-config/audit/oidc`` returns nothing
    for pre-rename history: ``get_audit_log`` filters on
    ``config_key IN CONFIG_CATEGORIES[category]``, and every stored key would still
    carry the old prefix.

**Ciphertext is carried across unchanged.** ``keycloak_client_secret`` is stored
encrypted under ``ENCRYPTION_KEY``; only ``config_key``, ``category`` and
``updated_at`` are written here. Nothing decrypts, re-encrypts, or even reads
``config_value`` — a rename must not depend on the encryption key being the same one
that wrote the row.

Environment variables are NOT affected: the retired ``KEYCLOAK_*`` names keep working
permanently, translated onto their ``OIDC_*`` counterparts by
``app/core/legacy_auth_env.py`` before ``Settings`` is built.

COMMUNITY EDITION: a deployment that never configured OIDC has no matching rows and
this revision is a complete no-op.

Revision ID: v377_rename_keycloak_config_to_oidc
Revises: v376_idp_group_mapping
Create Date: 2026-08-07
"""

from alembic import op

revision = "v377_rename_keycloak_config_to_oidc"
down_revision = "v376_idp_group_mapping"
branch_labels = None
depends_on = None

#: Length of the retired ``keycloak_`` prefix, +1 — ``substring(x from 10)`` returns
#: everything after it. Module-level so the consistency test can rebuild the expected
#: key without re-deriving the arithmetic.
LEGACY_PREFIX = "keycloak_"

#: Module-level so the consistency test can replay it against seeded rows. Written to
#: be re-runnable, which is also what makes it idempotent: after one pass no row
#: matches the WHERE clause.
RENAME_SQL = """
    -- auth_config.config_key is globally UNIQUE. The NOT EXISTS guard means a
    -- re-run, or a database that already carries the new spelling, is a no-op
    -- rather than a unique-violation.
    UPDATE auth_config
       SET config_key = 'oidc_' || substring(config_key from 10),
           category   = 'oidc',
           updated_at = now()
     WHERE config_key LIKE 'keycloak\\_%'
       AND NOT EXISTS (
           SELECT 1 FROM auth_config a2
            WHERE a2.config_key = 'oidc_' || substring(auth_config.config_key from 10)
       );

    -- Only the rows the UPDATE deliberately skipped, i.e. duplicates of a row that
    -- already exists under the new name. The new row is authoritative.
    DELETE FROM auth_config WHERE config_key LIKE 'keycloak\\_%';

    -- Any row still sitting in the old category (a key that did not carry the
    -- prefix) follows its tab.
    UPDATE auth_config SET category = 'oidc', updated_at = now()
     WHERE category = 'keycloak';

    -- History. auth_config_audit.config_key is not unique, so no guard is needed.
    UPDATE auth_config_audit
       SET config_key = 'oidc_' || substring(config_key from 10)
     WHERE config_key LIKE 'keycloak\\_%';
"""

#: The mirror image. Kept complete so a downgrade leaves a database an older backend
#: can read — the old code looks up ``keycloak_*`` keys and would otherwise see an
#: unconfigured provider.
REVERT_SQL = """
    UPDATE auth_config
       SET config_key = 'keycloak_' || substring(config_key from 6),
           category   = 'keycloak',
           updated_at = now()
     WHERE config_key LIKE 'oidc\\_%'
       AND NOT EXISTS (
           SELECT 1 FROM auth_config a2
            WHERE a2.config_key = 'keycloak_' || substring(auth_config.config_key from 6)
       );

    DELETE FROM auth_config WHERE config_key LIKE 'oidc\\_%';

    UPDATE auth_config SET category = 'keycloak', updated_at = now()
     WHERE category = 'oidc';

    UPDATE auth_config_audit
       SET config_key = 'keycloak_' || substring(config_key from 6)
     WHERE config_key LIKE 'oidc\\_%';
"""


def upgrade():
    op.execute(RENAME_SQL)


def downgrade():
    op.execute(REVERT_SQL)
