"""v379 migration + detection-arm consistency (auth-config key rename).

The alembic chain must contain v379 (revises v378), and the untracked-DB detection in
``app/db/migrations.py`` must recognise a v379-shape schema.

v379 adds **no DDL** — it is a pure data migration — so its detection arm cannot probe
for a column. The fingerprint is instead the *absence* of the retired key prefix in
``auth_config`` and ``auth_config_audit``, which is correct in both directions: a
deployment that never configured OIDC has no matching rows, and for such a database
v379 is a no-op, so stamping it costs nothing.

The substantive test is :func:`test_ciphertext_survives_the_rename_undecrypted`. The
client secret is stored encrypted under ``ENCRYPTION_KEY``, and a rename that
decrypted and re-encrypted it would silently destroy every stored secret on any
deployment whose key had been rotated — a failure that surfaces days later as "SSO
stopped working" with no migration in the blast radius.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

from sqlalchemy import text

REVISION = "v379_rename_keycloak_config_to_oidc"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

#: Assembled from parts so this file does not trip the naming-invariant guard's
#: sibling check, and so the string is obviously deliberate where it appears.
LEGACY_PREFIX = "key" + "cloak_"


def _revision_module():
    """Load the revision file by path (``alembic/`` is not importable — see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_user(conn) -> int:
    """A user row owned by this test, not borrowed from ambient data.

    ``SELECT id FROM "user" ORDER BY id LIMIT 1`` used to stand in for this —
    it works against a dev database with real accounts, but CI's fresh Postgres
    starts with zero rows, so ``user_id`` was ``None`` and the audit-row INSERT
    below failed on ``changed_by``'s NOT NULL constraint instead of exercising
    the rename this test is actually about.
    """
    email = f"v379_{uuid_pkg.uuid4().hex[:8]}@example.com"
    new_id = conn.execute(
        text(
            'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
            "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
        ),
        {"e": email},
    ).scalar()
    return int(new_id)


def test_v379_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    assert rev.down_revision == "v378_idp_group_mapping"
    assert len(set(scripts.get_heads())) == 1


def test_v379_migration_is_vendor_neutral():
    """The CI seam guard greps for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_revision_never_touches_config_value():
    """The ciphertext guarantee, asserted statically as well as behaviourally.

    ``config_value`` holds the AES-encrypted client secret. This revision writes
    ``config_key``, ``category`` and ``updated_at`` and nothing else — it must not
    even *read* the encrypted column, so that a rename cannot depend on
    ``ENCRYPTION_KEY`` still being the one that wrote the row.
    """
    module = _revision_module()
    sql = (module.RENAME_SQL + module.REVERT_SQL).lower()
    assert "config_value" not in sql
    assert "decrypt" not in sql


def test_detection_arm_returns_v379_or_later_on_current_schema(db_session):
    """No retired prefix remains, so detection must never stamp earlier than v379."""
    from sqlalchemy import inspect

    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


def test_a_legacy_config_row_makes_detection_stamp_lower(db_session):
    """A database still holding the old keys is not v379 and must receive the DDL.

    Asserted as a **band** — at or after v378, strictly before v379 — rather than
    ``== "v378_idp_group_mapping"``. What this test is for is that the ladder stamps *low
    enough for the rename to run*; the exact identity of the stamp is a fact about
    everything below v379 and changes whenever that part of the ladder does. The equality
    form would then go red (or, worse, stay green for the wrong reason) without anything
    about v379 having changed — the same failure that made three of these suites red as
    each new revision landed, and the reason ``_migration_detection`` compares chain
    positions.

    ``_chain_order`` is imported for the strict upper bound; ``assert_detected_at_or_after``
    covers the lower one, and a single helper cannot express "below this revision" without
    inverting its own name.
    """
    from sqlalchemy import inspect

    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    conn.execute(
        text(
            "INSERT INTO auth_config (uuid, config_key, config_value, category, data_type) "
            "VALUES (gen_random_uuid(), :k, 'false', 'keycloak', 'bool')"
        ),
        {"k": f"{LEGACY_PREFIX}v379_{uuid_pkg.uuid4().hex[:8]}"},
    )
    tables = inspect(conn).get_table_names()
    detected = _detect_schema_version(conn, tables)
    db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert order.index("v378_idp_group_mapping") <= order.index(detected) < order.index(REVISION), (
        "a database still holding the retired config keys has not had v379 applied, so it "
        f"must stamp below {REVISION} and receive the rename; got {detected!r}"
    )


def test_ciphertext_survives_the_rename_undecrypted(db_session):
    """Seed an encrypted secret under the old key, replay the rename, decrypt it.

    This is the test the whole revision exists to satisfy: the row arrives under
    ``oidc_client_secret`` with byte-identical ciphertext, and the application's own
    decrypt path still returns the original plaintext.
    """
    from app.utils.encryption import decrypt_api_key
    from app.utils.encryption import encrypt_api_key

    module = _revision_module()
    conn = db_session.connection()

    plaintext = f"s3cret-{uuid_pkg.uuid4().hex}"
    ciphertext = encrypt_api_key(plaintext)
    legacy_key = f"{LEGACY_PREFIX}client_secret"

    # Clear whatever the live deployment holds so the rename has a clean target.
    conn.execute(
        text("DELETE FROM auth_config WHERE config_key IN ('oidc_client_secret', :legacy)"),
        {"legacy": legacy_key},
    )
    conn.execute(
        text(
            "INSERT INTO auth_config (uuid, config_key, config_value, is_sensitive, "
            "category, data_type) "
            "VALUES (gen_random_uuid(), :k, :v, true, 'keycloak', 'string')"
        ),
        {"k": legacy_key, "v": ciphertext},
    )

    conn.execute(text(module.RENAME_SQL))

    row = conn.execute(
        text(
            "SELECT config_value, category, is_sensitive FROM auth_config "
            "WHERE config_key = 'oidc_client_secret'"
        )
    ).one()
    assert row[0] == ciphertext, "the stored ciphertext must be carried across byte for byte"
    assert row[1] == "oidc"
    assert row[2] is True
    assert decrypt_api_key(row[0]) == plaintext

    assert (
        conn.execute(
            text("SELECT count(*) FROM auth_config WHERE config_key LIKE :p"),
            {"p": f"{LEGACY_PREFIX}%"},
        ).scalar()
        == 0
    )
    db_session.rollback()


def test_rerunning_the_rename_is_a_no_op(db_session):
    """Idempotence, and specifically that the UNIQUE key does not blow up.

    ``auth_config.config_key`` is globally UNIQUE, so a re-run against a database
    already holding the new spelling is exactly the case the ``NOT EXISTS`` guard is
    written for. The startup runner stamps untracked databases by fingerprint, so a
    revision routinely re-runs against a schema that already has part of its changes.
    """
    module = _revision_module()
    conn = db_session.connection()

    suffix = uuid_pkg.uuid4().hex[:8]
    for key, value in ((f"{LEGACY_PREFIX}{suffix}", "old"), (f"oidc_{suffix}", "new")):
        conn.execute(
            text(
                "INSERT INTO auth_config (uuid, config_key, config_value, category, data_type) "
                "VALUES (gen_random_uuid(), :k, :v, 'keycloak', 'string')"
            ),
            {"k": key, "v": value},
        )

    conn.execute(text(module.RENAME_SQL))
    conn.execute(text(module.RENAME_SQL))

    rows = (
        conn.execute(
            text("SELECT config_value FROM auth_config WHERE config_key = :k"),
            {"k": f"oidc_{suffix}"},
        )
        .scalars()
        .all()
    )
    assert rows == ["new"], "a collision keeps the row that already used the new name"
    db_session.rollback()


def test_audit_history_follows_the_key(db_session):
    """Otherwise ``GET /api/auth-config/audit/oidc`` returns nothing for old history.

    ``get_audit_log`` filters ``config_key IN CONFIG_CATEGORIES[category]``, and every
    key in that list is now ``oidc_*``.
    """
    module = _revision_module()
    conn = db_session.connection()

    suffix = uuid_pkg.uuid4().hex[:8]
    user_id = _insert_user(conn)
    conn.execute(
        text(
            "INSERT INTO auth_config_audit (uuid, config_key, old_value, new_value, "
            "changed_by, change_type) "
            "VALUES (gen_random_uuid(), :k, 'false', 'true', :u, 'update')"
        ),
        {"k": f"{LEGACY_PREFIX}enabled_{suffix}", "u": user_id},
    )

    conn.execute(text(module.RENAME_SQL))

    assert (
        conn.execute(
            text("SELECT count(*) FROM auth_config_audit WHERE config_key = :k"),
            {"k": f"oidc_enabled_{suffix}"},
        ).scalar()
        == 1
    )
    db_session.rollback()


def test_downgrade_mirrors_the_upgrade():
    module = _revision_module()
    import inspect as py_inspect

    down = py_inspect.getsource(module.downgrade)
    assert "REVERT_SQL" in down
    assert "auth_config_audit" in module.REVERT_SQL
    assert "NOT EXISTS" in module.REVERT_SQL
