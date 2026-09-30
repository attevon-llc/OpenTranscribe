"""Tenant stamps for legacy speaker/vocabulary rows; per-tenant unique names (issue #1110).

Speaker profiles, speaker collections and custom-vocabulary terms carry
``organization_id`` (NULL = personal) and every read path now scopes on it. Two
things were left over from before those rows were stamped:

* **Legacy rows are unstamped.** A term or profile made by an organization member
  before stamping existed is NULL, so it now shows up only in that member's personal
  workspace, and a legacy profile whose voiceprints come from organization files would
  lose them on the next embedding recompute (that average only includes speakers of the
  profile's own tenant).
* **Names were unique per user across tenants** (``UNIQUE (user_id, name)`` on
  profiles and speaker collections, ``(COALESCE(user_id, 0), term, domain)`` on
  vocabulary). That blocks reusing a name in a second tenant and, by the error it
  returns, confirms that the name exists in the other tenant.

Backfill (``STAMP_STATEMENTS``; re-runnable, only ever fills a NULL)
--------------------------------------------------------------------
A row is stamped only when its tenant is unambiguous:

1. **Profiles with linked speakers** take the tenant of the files those speakers are
   in, when every such file is in one and the same organization.
2. **Profiles without linked speakers, speaker collections without members, and a
   user's own vocabulary terms** take their owner's tenant when the owner has exactly
   one: one organization (membership or files) and no personal files.
3. **Speaker collections with members** take the tenant of their member profiles
   (after step 1), when every member is in one and the same organization.

Rows whose evidence is all personal stay personal and are not reported. Rows whose
evidence spans tenants are left NULL and listed by ``AMBIGUOUS_REPORT_SQL`` (reason
``linked_files_span_tenants``, ``member_profiles_span_tenants`` or
``owner_in_several_tenants``, with the candidate tenants) —
``python -m app.scripts.backfill_tenant_stamps`` prints that report. Owner-less
vocabulary terms (instance-wide) are never touched.

Every statement takes a ``:cutoff`` and only considers rows created before it. The
upgrade passes ``infinity`` (every existing row is legacy); the command requires an
explicit cutoff so a re-run cannot move rows a user created in the personal workspace
after the deploy.

Schema
------
* ``speaker_profile``: ``UNIQUE (user_id, name)`` (found by columns: live databases
  carry ``speaker_profile_user_id_name_key``) → ``uq_speaker_profile_user_tenant_name``
  ``(user_id, COALESCE(organization_id, 0), name)``.
* ``speaker_collection``: same shape, ``uq_speaker_collection_user_tenant_name``.
* ``custom_vocabulary``: ``_custom_vocab_unique`` → ``uq_custom_vocab_user_tenant_term``
  ``(COALESCE(user_id, 0), COALESCE(organization_id, 0), term, domain)``.

``COALESCE(organization_id, 0)`` makes the personal workspace one tenant for
uniqueness (organization ids start at 1), so two personal rows of one name still
collide — a plain column list would let NULLs through.

COMMUNITY EDITION: no organizations, every row personal, so nothing is stamped and the
new indexes enforce exactly the old rule.

Downgrade
---------
Drops the new indexes and restores the old per-user uniques, each only when the data
still satisfies it (a name a user now holds in two tenants cannot be represented in the
old shape and is left alone rather than deleted or renamed — the ``v422`` stance). The
tenant stamps are kept: they are correct data, and the old schema has the column.

Revision ID: v430_per_tenant_speaker_and_vocab_names
Revises: v422_add_collection_tenancy
Create Date: 2026-09-30
"""

from alembic import op

revision = "v430_per_tenant_speaker_and_vocab_names"
down_revision = "v422_add_collection_tenancy"
branch_labels = None
depends_on = None

#: A user's tenant evidence: distinct organizations (memberships + files) and whether
#: they own personal files. Unambiguous = exactly one organization, no personal files.
_OWNER_TENANT_CTE = """
    owner_tenant AS (
        SELECT ev.user_id,
               MIN(ev.org_id) AS org_id,
               COUNT(DISTINCT ev.org_id) AS n_orgs,
               BOOL_OR(ev.personal) AS has_personal,
               ARRAY_AGG(DISTINCT ev.org_id) AS candidates
          FROM (
                SELECT user_id, organization_id AS org_id, false AS personal
                  FROM organization_membership
                UNION ALL
                SELECT user_id, organization_id, organization_id IS NULL
                  FROM media_file
               ) AS ev
         GROUP BY ev.user_id
    )
"""

_PROFILE_FILES_CTE = """
    profile_files AS (
        SELECT s.profile_id,
               MIN(mf.organization_id) AS org_id,
               COUNT(DISTINCT mf.organization_id) AS n_orgs,
               BOOL_OR(mf.organization_id IS NULL) AS has_personal,
               ARRAY_AGG(DISTINCT mf.organization_id) AS candidates
          FROM speaker s
          JOIN media_file mf ON mf.id = s.media_file_id
         WHERE s.profile_id IS NOT NULL
         GROUP BY s.profile_id
    )
"""

_COLLECTION_MEMBERS_CTE = """
    collection_members AS (
        SELECT m.collection_id,
               MIN(p.organization_id) AS org_id,
               COUNT(DISTINCT p.organization_id) AS n_orgs,
               BOOL_OR(p.organization_id IS NULL) AS has_personal,
               ARRAY_AGG(DISTINCT p.organization_id) AS candidates
          FROM speaker_collection_member m
          JOIN speaker_profile p ON p.id = m.speaker_profile_id
         GROUP BY m.collection_id
    )
"""

_BOUND_CUTOFF = "CAST(:cutoff AS timestamptz)"


def _stamp_statements(cutoff: str) -> tuple[str, ...]:
    """The stamping UPDATEs, in order, for rows created before ``cutoff`` (SQL text).

    Profiles come before collections, whose evidence is their member profiles' stamps.
    """
    legacy = "COALESCE({alias}.created_at, '-infinity') < " + cutoff
    return (
        f"""
        WITH {_PROFILE_FILES_CTE}
        UPDATE speaker_profile p
           SET organization_id = pf.org_id
          FROM profile_files pf
         WHERE p.id = pf.profile_id
           AND p.organization_id IS NULL
           AND pf.n_orgs = 1 AND NOT pf.has_personal
           AND {legacy.format(alias="p")}
        """,
        f"""
        WITH {_OWNER_TENANT_CTE}
        UPDATE speaker_profile p
           SET organization_id = ot.org_id
          FROM owner_tenant ot
         WHERE p.user_id = ot.user_id
           AND p.organization_id IS NULL
           AND ot.n_orgs = 1 AND NOT ot.has_personal
           AND NOT EXISTS (SELECT 1 FROM speaker s WHERE s.profile_id = p.id)
           AND {legacy.format(alias="p")}
        """,
        f"""
        WITH {_COLLECTION_MEMBERS_CTE}
        UPDATE speaker_collection c
           SET organization_id = cm.org_id
          FROM collection_members cm
         WHERE c.id = cm.collection_id
           AND c.organization_id IS NULL
           AND cm.n_orgs = 1 AND NOT cm.has_personal
           AND {legacy.format(alias="c")}
        """,
        f"""
        WITH {_OWNER_TENANT_CTE}
        UPDATE speaker_collection c
           SET organization_id = ot.org_id
          FROM owner_tenant ot
         WHERE c.user_id = ot.user_id
           AND c.organization_id IS NULL
           AND ot.n_orgs = 1 AND NOT ot.has_personal
           AND NOT EXISTS (
                SELECT 1 FROM speaker_collection_member m WHERE m.collection_id = c.id
           )
           AND {legacy.format(alias="c")}
        """,
        f"""
        WITH {_OWNER_TENANT_CTE}
        UPDATE custom_vocabulary v
           SET organization_id = ot.org_id
          FROM owner_tenant ot
         WHERE v.user_id = ot.user_id
           AND v.organization_id IS NULL
           AND ot.n_orgs = 1 AND NOT ot.has_personal
           AND {legacy.format(alias="v")}
        """,
    )


#: Executed in order with a ``:cutoff`` bind (``timestamptz``; ``'infinity'`` = all).
STAMP_STATEMENTS: tuple[str, ...] = _stamp_statements(_BOUND_CUTOFF)

_REPORT_LEGACY = "COALESCE({alias}.created_at, '-infinity') < " + _BOUND_CUTOFF

#: Unstamped rows whose evidence spans tenants. Takes the same ``:cutoff`` bind.
#: Columns: kind, id, user_id, label, reason, candidates (int[]; NULL = personal).
AMBIGUOUS_REPORT_SQL = f"""
    WITH {_OWNER_TENANT_CTE}, {_PROFILE_FILES_CTE}, {_COLLECTION_MEMBERS_CTE}
    SELECT 'profile' AS kind, p.id, p.user_id, p.name AS label,
           'linked_files_span_tenants' AS reason, pf.candidates
      FROM speaker_profile p
      JOIN profile_files pf ON pf.profile_id = p.id
     WHERE p.organization_id IS NULL
       AND pf.n_orgs + pf.has_personal::int > 1
       AND {_REPORT_LEGACY.format(alias="p")}
    UNION ALL
    SELECT 'profile', p.id, p.user_id, p.name,
           'owner_in_several_tenants', ot.candidates
      FROM speaker_profile p
      JOIN owner_tenant ot ON ot.user_id = p.user_id
     WHERE p.organization_id IS NULL
       AND ot.n_orgs + ot.has_personal::int > 1
       AND NOT EXISTS (SELECT 1 FROM speaker s WHERE s.profile_id = p.id)
       AND {_REPORT_LEGACY.format(alias="p")}
    UNION ALL
    SELECT 'speaker_collection', c.id, c.user_id, c.name,
           'member_profiles_span_tenants', cm.candidates
      FROM speaker_collection c
      JOIN collection_members cm ON cm.collection_id = c.id
     WHERE c.organization_id IS NULL
       AND cm.n_orgs + cm.has_personal::int > 1
       AND {_REPORT_LEGACY.format(alias="c")}
    UNION ALL
    SELECT 'speaker_collection', c.id, c.user_id, c.name,
           'owner_in_several_tenants', ot.candidates
      FROM speaker_collection c
      JOIN owner_tenant ot ON ot.user_id = c.user_id
     WHERE c.organization_id IS NULL
       AND ot.n_orgs + ot.has_personal::int > 1
       AND NOT EXISTS (
            SELECT 1 FROM speaker_collection_member m WHERE m.collection_id = c.id
       )
       AND {_REPORT_LEGACY.format(alias="c")}
    UNION ALL
    SELECT 'vocabulary', v.id, v.user_id, v.term,
           'owner_in_several_tenants', ot.candidates
      FROM custom_vocabulary v
      JOIN owner_tenant ot ON ot.user_id = v.user_id
     WHERE v.organization_id IS NULL
       AND ot.n_orgs + ot.has_personal::int > 1
       AND {_REPORT_LEGACY.format(alias="v")}
    ORDER BY 1, 2
"""

#: The per-user uniques go by columns, not by name (the ORM and live databases
#: disagree on the constraint names). The detection arm keys on
#: ``uq_speaker_profile_user_tenant_name``, created LAST.
RESHAPE_SQL = """
    DO $$
    DECLARE
        con RECORD;
    BEGIN
        FOR con IN
            SELECT c.conrelid::regclass AS tbl, c.conname
              FROM pg_constraint c
             WHERE c.conrelid IN ('speaker_profile'::regclass, 'speaker_collection'::regclass)
               AND c.contype = 'u'
               AND (SELECT array_agg(a.attname::text ORDER BY a.attname)
                      FROM unnest(c.conkey) AS k(attnum)
                      JOIN pg_attribute a
                        ON a.attrelid = c.conrelid AND a.attnum = k.attnum)
                   = ARRAY['name', 'user_id']
        LOOP
            EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', con.tbl, con.conname);
        END LOOP;
    END $$;

    DROP INDEX IF EXISTS _custom_vocab_unique;

    CREATE UNIQUE INDEX IF NOT EXISTS uq_custom_vocab_user_tenant_term
        ON custom_vocabulary (COALESCE(user_id, 0), COALESCE(organization_id, 0), term, domain);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_speaker_collection_user_tenant_name
        ON speaker_collection (user_id, COALESCE(organization_id, 0), name);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_speaker_profile_user_tenant_name
        ON speaker_profile (user_id, COALESCE(organization_id, 0), name);
"""

#: Every existing row is legacy at upgrade time. Kept as ``A + B`` of named strings so
#: the DDL-marker scanner (tests/unit/test_ddl_marker_discipline.py) can resolve it.
_ALL_ROWS_STATEMENTS = _stamp_statements("CAST('infinity' AS timestamptz)")
_STAMP_ALL_SQL = f"{';'.join(_ALL_ROWS_STATEMENTS)};\n"

UPGRADE_SQL = _STAMP_ALL_SQL + RESHAPE_SQL

DOWNGRADE_SQL = """
    DROP INDEX IF EXISTS uq_speaker_profile_user_tenant_name;
    DROP INDEX IF EXISTS uq_speaker_collection_user_tenant_name;
    DROP INDEX IF EXISTS uq_custom_vocab_user_tenant_term;

    -- The old shape, restored only where the data still satisfies it: a name a user
    -- now holds in two tenants is neither deleted nor renamed.
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM speaker_profile GROUP BY user_id, name HAVING COUNT(*) > 1
        ) AND NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'speaker_profile_user_id_name_key'
        ) THEN
            ALTER TABLE speaker_profile
                ADD CONSTRAINT speaker_profile_user_id_name_key UNIQUE (user_id, name);
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM speaker_collection GROUP BY user_id, name HAVING COUNT(*) > 1
        ) AND NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'speaker_collection_user_id_name_key'
        ) THEN
            ALTER TABLE speaker_collection
                ADD CONSTRAINT speaker_collection_user_id_name_key UNIQUE (user_id, name);
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM custom_vocabulary
             GROUP BY COALESCE(user_id, 0), term, domain HAVING COUNT(*) > 1
        ) THEN
            CREATE UNIQUE INDEX IF NOT EXISTS _custom_vocab_unique
                ON custom_vocabulary (COALESCE(user_id, 0), term, domain);
        END IF;
    END $$;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
