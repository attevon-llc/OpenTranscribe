"""v0.3.0 - Directory (LDAP / Active Directory) login columns on "user"

Revision ID: v030_add_ldap_auth
Revises: v020_add_system_settings

Gives every account an authentication source and a directory identity:

* ``auth_type`` VARCHAR(10) NOT NULL, server default ``'local'``: which method
  authenticates the account (later revisions widen and constrain it).
* ``ldap_uid`` VARCHAR(255), nullable, indexed (``idx_user_ldap_uid``) and unique
  (``uq_user_ldap_uid``): the value of the configured LDAP username attribute for the
  account, whatever attribute the deployment chose.

Each step first asks the catalog whether its object is already there, so the revision
can run against a database that already has some of it. A database created from
``database/init_db.sql`` already has ``ldap_uid`` declared UNIQUE under a generated
constraint name; the named constraint is therefore added only when this revision
itself created the column.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "v030_add_ldap_auth"
down_revision = "v020_add_system_settings"
branch_labels = None
depends_on = None

_TABLE = "user"
_INDEX = "idx_user_ldap_uid"
_UNIQUE = "uq_user_ldap_uid"

_HAS_COLUMN = sa.text(
    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
    "WHERE table_name = 'user' AND column_name = :column)"
)
_HAS_INDEX = sa.text(
    "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE tablename = 'user' AND indexname = :index)"
)
_HAS_CONSTRAINT = sa.text(
    "SELECT EXISTS (SELECT 1 FROM information_schema.table_constraints "
    "WHERE table_name = 'user' AND constraint_name = :constraint)"
)


def _column_present(column: str) -> bool:
    return bool(op.get_bind().execute(_HAS_COLUMN, {"column": column}).scalar())


def _index_present(index: str) -> bool:
    return bool(op.get_bind().execute(_HAS_INDEX, {"index": index}).scalar())


def _constraint_present(constraint: str) -> bool:
    return bool(op.get_bind().execute(_HAS_CONSTRAINT, {"constraint": constraint}).scalar())


def upgrade():
    """Add ``auth_type`` and ``ldap_uid`` with its index and unique constraint."""
    if not _column_present("auth_type"):
        op.add_column(
            _TABLE,
            sa.Column("auth_type", sa.String(10), nullable=False, server_default="local"),
        )

    created_ldap_uid = not _column_present("ldap_uid")
    if created_ldap_uid:
        op.add_column(_TABLE, sa.Column("ldap_uid", sa.String(255), nullable=True))

    if not _index_present(_INDEX):
        op.create_index(_INDEX, _TABLE, ["ldap_uid"])

    if created_ldap_uid and not _constraint_present(_UNIQUE):
        op.create_unique_constraint(_UNIQUE, _TABLE, ["ldap_uid"])


def downgrade():
    """Remove what ``upgrade`` adds, skipping anything already gone."""
    if _constraint_present(_UNIQUE):
        op.drop_constraint(_UNIQUE, _TABLE, type_="unique")
    if _index_present(_INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _column_present("ldap_uid"):
        op.drop_column(_TABLE, "ldap_uid")
    if _column_present("auth_type"):
        op.drop_column(_TABLE, "auth_type")
