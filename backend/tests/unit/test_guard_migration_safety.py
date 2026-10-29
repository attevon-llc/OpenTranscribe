"""Tests for ``scripts/guards/check_migration_safety.py`` (risky operations in new revisions)."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "guards" / "check_migration_safety.py"
_REAL_ROOT = _SCRIPT.parents[2]
REASON = "reviewed: table is empty at upgrade time"
LOCK = "    op.execute(\"SET LOCAL lock_timeout = '30s'\")\n"


def _load():
    spec = importlib.util.spec_from_file_location("_check_migration_safety_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


def _rules(src: str) -> list[str]:
    return sorted(r for r, _ in guard.findings_for(ast.parse(src)))


def _up(body: str, lock: bool = True) -> str:
    """A revision whose upgrade() runs ``body`` (indented), by default with a lock_timeout."""
    return "def upgrade():\n" + (LOCK if lock else "") + body + "\ndef downgrade():\n    pass\n"


def _sql(stmt: str, lock: bool = True) -> str:
    return _up(f"    op.execute({stmt!r})\n", lock)


# --- each rule fires on a planted statement and stays clean on the safe spelling ----------------


@pytest.mark.parametrize(
    ("rule", "bad", "good"),
    [
        (
            "R1",
            "CREATE INDEX ix ON media_file (title)",
            "CREATE INDEX CONCURRENTLY ix ON media_file (title)",
        ),
        (
            "R1",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix ON media_file (title)",
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix ON media_file (title)",
        ),
        ("R2", "ALTER TABLE t DROP COLUMN c", "ALTER TABLE t ADD COLUMN c integer"),
        ("R2", "DROP TABLE old_things", "DROP INDEX old_ix"),
        (
            "R3",
            "ALTER TABLE t ADD COLUMN c integer NOT NULL",
            "ALTER TABLE t ADD COLUMN c integer NOT NULL DEFAULT 0",
        ),
        (
            "R3",
            "ALTER TABLE t ADD COLUMN IF NOT EXISTS c integer NOT NULL, ADD COLUMN d integer",
            "ALTER TABLE t ADD COLUMN c integer, ADD COLUMN d integer NOT NULL DEFAULT 1",
        ),
        (
            "R4",
            "ALTER TABLE t ALTER COLUMN c SET NOT NULL",
            "ALTER TABLE t ALTER COLUMN c DROP NOT NULL",
        ),
        ("R4", "ALTER TABLE t ALTER COLUMN c TYPE bigint", "ALTER TYPE mood ADD VALUE 'x'"),
        ("R4", "ALTER TABLE t ALTER c SET DATA TYPE text", "ALTER TABLE t ALTER c SET DEFAULT 'x'"),
        (
            "R5",
            "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (p) REFERENCES parent (id)",
            "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (p) REFERENCES parent (id) NOT VALID",
        ),
        (
            "R5",
            "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0)",
            "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0) NOT VALID",
        ),
    ],
)
def test_sql_rule_fires_on_bad_and_not_on_good(rule, bad, good):
    assert _rules(_sql(bad)) == [rule]
    assert _rules(_sql(good)) == []


def test_op_forms_of_r1_to_r5():
    cases = {
        "R1": (
            "op.create_index('ix', 'big', ['c'])",
            "op.create_index('ix', 'big', ['c'], postgresql_concurrently=True)",
        ),
        "R2": ("op.drop_column('t', 'c')", "op.drop_index('ix')"),
        "R3": (
            "op.add_column('t', sa.Column('c', sa.Integer(), nullable=False))",
            "op.add_column('t', sa.Column('c', sa.Integer(), nullable=False, server_default='0'))",
        ),
        "R4": (
            "op.alter_column('t', 'c', nullable=False)",
            "op.alter_column('t', 'c', nullable=True)",
        ),
        "R5": (
            "op.create_foreign_key('fk', 'child', 'parent', ['p'], ['id'])",
            "op.create_index('ix', 'new', ['c'])\n    op.create_table('new', sa.Column('c', sa.Integer))",
        ),
    }
    for rule, (bad, good) in cases.items():
        assert _rules(_up(f"    {bad}\n")) == [rule], rule
        assert _rules(_up(f"    {good}\n")) == [], rule


def test_remaining_op_spellings():
    assert _rules(_up("    op.drop_table('t')\n")) == ["R2"]
    assert _rules(_up("    op.alter_column('t', 'c', type_=sa.Text())\n")) == ["R4"]
    assert _rules(_up("    op.create_check_constraint('ck', 't', 'c > 0')\n")) == ["R5"]
    assert _rules(
        _up("    with op.batch_alter_table('t') as b:\n        b.drop_column('c')\n")
    ) == ["R2"]
    batch = "    with op.batch_alter_table('t') as b:\n        b.add_column(sa.Column('c', sa.Integer, nullable=False))\n"
    assert _rules(_up(batch)) == ["R3"]


def test_server_default_none_counts_as_no_default():
    src = "    op.add_column('t', sa.Column('c', sa.Integer(), nullable=False, server_default=None))\n"
    assert _rules(_up(src)) == ["R3"]


def test_nullable_add_column_and_nullable_true_are_clean():
    assert _rules(_up("    op.add_column('t', sa.Column('c', sa.Integer()))\n")) == []


def test_tables_created_in_this_revision_are_exempt():
    create = "    op.create_table('n', sa.Column('p', sa.Integer))\n"
    for stmt in (
        "op.create_index('ix', 'n', ['p'])",
        "op.create_foreign_key('fk', 'n', 'parent', ['p'], ['id'])",
        "op.add_column('n', sa.Column('c', sa.Integer(), nullable=False))",
        "op.alter_column('n', 'c', nullable=False)",
    ):
        assert _rules(_up(create + f"    {stmt}\n")) == [], stmt
    sql_create = _up(
        "    op.execute('CREATE TABLE n (id integer)')\n"
        "    op.execute('CREATE INDEX ix ON n (id)')\n"
        "    op.execute('ALTER TABLE n ADD COLUMN c integer NOT NULL')\n"
    )
    assert _rules(sql_create) == []


def test_other_tables_are_not_exempted_by_a_new_table():
    src = _up(
        "    op.create_table('n', sa.Column('p', sa.Integer))\n    op.create_index('ix', 'big', ['p'])\n"
    )
    assert _rules(src) == ["R1"]


# --- R6 ---------------------------------------------------------------------------------------


def test_r6_fires_with_risky_op_and_no_lock_timeout():
    assert _rules(_sql("CREATE INDEX ix ON media_file (title)", lock=False)) == ["R1", "R6"]
    assert _rules(_sql("LOCK TABLE speaker IN SHARE ROW EXCLUSIVE MODE", lock=False)) == ["R6"]


def test_r6_clean_with_lock_timeout_and_without_risky_ops():
    assert _rules(_sql("LOCK TABLE speaker IN SHARE ROW EXCLUSIVE MODE")) == []
    assert _rules(_sql("UPDATE t SET c = 1", lock=False)) == []
    assert _rules(_up("    pass\n", lock=False)) == []


def test_r6_counts_once_per_revision():
    src = _up("    op.drop_table('a')\n    op.drop_table('b')\n", lock=False)
    assert _rules(src) == ["R2", "R2", "R6"]


def test_lock_timeout_set_through_a_module_constant_counts():
    src = (
        "LOCK_SQL = \"SET LOCAL lock_timeout = '30s'\"\n"
        "def upgrade():\n    op.execute(LOCK_SQL)\n    op.drop_table('a')\n"
    )
    assert _rules(src) == ["R2"]


def test_the_probe_plant_and_its_fix():
    bad = 'def upgrade():\n    op.execute("CREATE INDEX ix_probe ON media_file (title)")\n'
    assert _rules(bad) == ["R1", "R6"]
    good = (
        "def upgrade():\n"
        "    op.execute(\"SET LOCAL lock_timeout = '30s'\")\n"
        "    with op.get_context().autocommit_block():\n"
        '        op.execute("CREATE INDEX CONCURRENTLY ix_probe ON media_file (title)")\n'
    )
    assert _rules(good) == []


# --- DYNAMIC-SQL and SQL reachability ----------------------------------------------------------


def test_dynamic_sql_is_flagged_and_literal_parts_still_checked():
    fstr = _up("    op.execute(f'DROP TABLE {name}')\n")
    assert _rules(fstr) == ["DYNAMIC-SQL", "R2"]
    concat = _up("    op.execute('CREATE INDEX ix ON ' + table + ' (c)')\n")
    assert _rules(concat) == ["DYNAMIC-SQL", "R1"]
    assert _rules(_up("    op.execute('SELECT {}'.format(x))\n")) == ["DYNAMIC-SQL"]
    assert _rules(_up("    op.execute(sa.text(f'SELECT {x}'))\n")) == ["DYNAMIC-SQL"]


def test_static_sql_is_not_dynamic():
    assert _rules(_sql("UPDATE t SET c = 1")) == []


def test_sql_in_module_constants_and_text_variables_is_followed_once():
    src = (
        "STMT = 'ALTER TABLE t DROP COLUMN c'\n"
        "def upgrade():\n    op.execute(\"SET LOCAL lock_timeout = '1s'\")\n    op.execute(STMT)\n"
    )
    assert _rules(src) == ["R2"]
    via_text = _up("    stmt = sa.text('DROP TABLE x')\n    conn.execute(stmt)\n")
    assert _rules(via_text) == ["R2"]
    assert _rules(_up("    op.execute(sa.text('DROP TABLE x'))\n")) == ["R2"]
    assert _rules(_up("    conn.exec_driver_sql('DROP TABLE x')\n")) == ["R2"]


def test_sql_comments_are_ignored():
    assert _rules(_sql("-- DROP TABLE x\nUPDATE t SET c = 1")) == []


def test_multiple_statements_and_do_blocks():
    src = _sql("DROP TABLE a; ALTER TABLE t ALTER COLUMN c SET NOT NULL")
    assert _rules(src) == ["R2", "R4"]
    block = "DO $$ BEGIN ALTER TABLE t ADD COLUMN c integer NOT NULL; END $$"
    assert _rules(_sql(block)) == ["R3"]


def test_downgrade_is_not_counted():
    src = (
        "def upgrade():\n    pass\n"
        "def downgrade():\n    op.drop_table('x')\n    op.execute('CREATE INDEX ix ON t (c)')\n"
        "    op.alter_column('t', 'c', nullable=False)\n"
    )
    assert _rules(src) == []
    assert (
        _rules("async def upgrade():\n    pass\nasync def downgrade():\n    op.drop_table('x')\n")
        == []
    )
    assert _rules("op.drop_table('x')\ndef downgrade():\n    op.drop_table('y')\n") == ["R2", "R6"]


def test_helpers_async_and_engine_dispatch_are_followed():
    helper = "def _drop():\n    op.drop_table('x')\ndef upgrade():\n    _drop()\n"
    assert _rules(helper) == ["R2", "R6"]
    assert _rules("def _unused():\n    op.drop_table('x')\ndef upgrade():\n    pass\n") == []
    multi = "def upgrade(engine_name):\n    pass\ndef upgrade_main():\n    op.drop_table('x')\ndef downgrade_main():\n    op.drop_table('y')\n"
    assert _rules(multi) == ["R2", "R6"]
    assert _rules("async def upgrade():\n    op.drop_table('x')\n") == ["R2", "R6"]


# --- command line: baseline, fail-closed -------------------------------------------------------


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = tmp_path / "backend" / "alembic" / "versions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _run(root: Path, *extra: str) -> int:
    code: int = guard.main(["--root", str(root), "--baseline", str(root / "baseline.txt"), *extra])
    return code


def _write_baseline(root: Path) -> None:
    assert _run(root, "--write-baseline", "--reason", REASON) == 0


PROBE = "v999_probe.py"
DROP_ONE = _up("    op.drop_table('a')\n")
DROP_TWO = _up("    op.drop_table('a')\n    op.drop_table('b')\n")
KEY = "backend/alembic/versions/v999_probe.py::R2"


def test_planted_revision_fails_against_empty_baseline(tmp_path, capsys):
    root = _repo(tmp_path, {PROBE: DROP_ONE})
    (root / "baseline.txt").write_text("")
    assert _run(root) == 1
    assert KEY in capsys.readouterr().err


def test_safe_revision_passes_against_empty_baseline(tmp_path):
    safe = _up(
        "    with op.get_context().autocommit_block():\n"
        "        op.execute('CREATE INDEX CONCURRENTLY ix ON t (c)')\n"
    )
    root = _repo(tmp_path, {PROBE: safe})
    (root / "baseline.txt").write_text("")
    assert _run(root) == 0


def test_scan_counts_hits_per_rule(tmp_path):
    root = _repo(tmp_path, {PROBE: DROP_TWO})
    assert guard.scan(root) == {KEY: 2}


def test_grown_count_fails_and_matching_baseline_passes(tmp_path):
    root = _repo(tmp_path, {PROBE: DROP_ONE})
    _write_baseline(root)
    assert _run(root) == 0
    (root / "backend/alembic/versions" / PROBE).write_text(DROP_TWO)
    assert _run(root) == 1


def test_loosened_and_stale_entries_fail_and_tighten_repairs(tmp_path):
    root = _repo(tmp_path, {PROBE: DROP_TWO, "v998_old.py": DROP_ONE})
    _write_baseline(root)
    _repo(root, {PROBE: DROP_ONE, "v998_old.py": "def upgrade():\n    pass\n"})
    assert _run(root) == 1
    assert _run(root, "--tighten") == 0
    assert _run(root) == 0
    text = (root / "baseline.txt").read_text()
    assert "v998_old" not in text
    assert f"{KEY}\t1\t{REASON}" in text


def test_tighten_never_adds_a_new_violation(tmp_path):
    root = _repo(tmp_path, {PROBE: DROP_ONE})
    (root / "baseline.txt").write_text("")
    assert _run(root, "--tighten") == 1


def test_missing_and_empty_versions_dir_fail_closed(tmp_path, capsys):
    assert _run(tmp_path) == 2
    (tmp_path / "backend" / "alembic" / "versions").mkdir(parents=True)
    assert _run(tmp_path) == 2
    assert "no revisions" in capsys.readouterr().err


def test_syntax_error_fails_closed(tmp_path, capsys):
    root = _repo(tmp_path, {PROBE: "def upgrade(:\n"})
    (root / "baseline.txt").write_text("")
    assert _run(root) == 2
    assert "cannot parse" in capsys.readouterr().err


def test_write_baseline_requires_real_reason(tmp_path):
    root = _repo(tmp_path, {PROBE: DROP_ONE})
    assert _run(root, "--write-baseline", "--reason", "TODO") == 2


def test_real_repo_passes_with_committed_baseline():
    assert (_REAL_ROOT / "backend" / "alembic" / "versions").is_dir()
    assert guard.main(["--root", str(_REAL_ROOT)]) == 0
