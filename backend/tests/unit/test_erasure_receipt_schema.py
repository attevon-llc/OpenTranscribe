"""The erasure receipt table cannot hold personal data (issue #1353, plan test R3).

Same rule as the ledger: the receipt that proves a deletion must not become a copy of what was
deleted. So the table has no free-text column, and each map is nailed shut by two CHECKs (values
are JSON numbers, keys are logical store names). These tests INSERT against the real constraints.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

import pytest
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.erasure import RECEIPT_MAP_COLUMNS

ENUM_COLUMNS = {"phase", "outcome", "digest"}
GOOD_DIGEST = "a" * 64


def _ledger(conn) -> int:
    new_id = conn.execute(
        text(
            "INSERT INTO erasure_ledger (uuid, subject_type, subject_user_id, sla_due_at) "
            "VALUES (:u, 'user', 99001, now() + interval '30 days') RETURNING id"
        ),
        {"u": str(uuid_pkg.uuid4())},
    ).scalar_one()
    return int(new_id)


def _insert(conn, ledger_id: int, **overrides):
    params = {
        "u": str(uuid_pkg.uuid4()),
        "l": ledger_id,
        "phase": "immediate",
        "outcome": "verified",
        "digest": GOOD_DIGEST,
        **{c: "{}" for c in RECEIPT_MAP_COLUMNS},
    }
    params.update(overrides)
    maps = ", ".join(f"CAST(:{c} AS jsonb)" for c in RECEIPT_MAP_COLUMNS)
    return conn.execute(
        text(
            "INSERT INTO erasure_receipt (uuid, ledger_entry_id, attempt, phase, outcome, "
            f"{', '.join(RECEIPT_MAP_COLUMNS)}, inventory_version, digest) "
            f"VALUES (:u, :l, 1, :phase, :outcome, {maps}, 1, :digest) RETURNING id"
        ),
        params,
    ).scalar_one()


def test_the_receipt_has_no_free_text_column(db_session):
    columns = inspect(db_session.connection()).get_columns("erasure_receipt")
    textual = {
        c["name"]
        for c in columns
        if isinstance(c["type"], (String, Text)) and c["name"] not in ENUM_COLUMNS
    }
    assert textual == set(), f"free-text columns could hold personal data: {textual}"
    assert {c["name"] for c in columns} >= set(RECEIPT_MAP_COLUMNS)


@pytest.mark.parametrize("column", RECEIPT_MAP_COLUMNS)
def test_a_string_value_is_rejected_in_every_map(db_session, column):
    conn = db_session.connection()
    ledger_id = _ledger(conn)
    with pytest.raises(IntegrityError), conn.begin_nested():
        _insert(conn, ledger_id, **{column: json.dumps({"pg.media_file": "alice@example.com"})})


@pytest.mark.parametrize("column", RECEIPT_MAP_COLUMNS)
@pytest.mark.parametrize("key", ["alice@example.com", "pg", "pg.", "PG.media_file", "s4.x"])
def test_a_key_that_is_not_a_logical_store_name_is_rejected(db_session, column, key):
    conn = db_session.connection()
    ledger_id = _ledger(conn)
    with pytest.raises(IntegrityError), conn.begin_nested():
        _insert(conn, ledger_id, **{column: json.dumps({key: 3})})


def test_a_store_key_with_a_number_is_accepted(db_session):
    conn = db_session.connection()
    row = _insert(conn, _ledger(conn), deleted=json.dumps({"pg.media_file": 3, "os.speakers": 0}))
    assert row >= 1


def test_a_nested_value_is_not_a_way_around_the_number_rule(db_session):
    conn = db_session.connection()
    with pytest.raises(IntegrityError), conn.begin_nested():
        _insert(conn, _ledger(conn), deleted=json.dumps({"pg.media_file": {"name": "x"}}))


@pytest.mark.parametrize("field", ["phase", "outcome", "digest"])
def test_enum_columns_reject_a_free_value(db_session, field):
    conn = db_session.connection()
    with pytest.raises(IntegrityError), conn.begin_nested():
        _insert(conn, _ledger(conn), **{field: "bogus"})


def test_a_ledger_entry_cannot_vanish_under_its_receipts(db_session):
    conn = db_session.connection()
    ledger_id = _ledger(conn)
    _insert(conn, ledger_id)
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(text("DELETE FROM erasure_ledger WHERE id = :i"), {"i": ledger_id})


def test_the_ledger_accepts_a_file_subject_only_with_its_uuid(db_session):
    conn = db_session.connection()
    insert = (
        "INSERT INTO erasure_ledger (uuid, subject_type, subject_file_uuid, sla_due_at) "
        "VALUES (:u, 'file', :f, now() + interval '30 days')"
    )
    conn.execute(text(insert), {"u": str(uuid_pkg.uuid4()), "f": str(uuid_pkg.uuid4())})
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(text(insert), {"u": str(uuid_pkg.uuid4()), "f": None})
