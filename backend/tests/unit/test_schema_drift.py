"""Model-vs-schema drift, as a gate on the categories that actually break things.

Deliberately NOT "assert compare_metadata() is empty". A recon run against a
migrated database returned ~395 diffs, and they are not 395 bugs — they are
dominated by two systemic, cosmetic causes documented in
``scripts/check-schema-drift.py``:

* ~295 index diffs, because hand-written raw-SQL migrations name indexes
  ``idx_<table>_<col>`` while SQLAlchemy's implicit name for ``index=True`` is
  ``ix_<table>_<col>``. Both indexes exist and cover the same column.
* ~75 constraint diffs, same naming story. ``backend/app/db/base.py`` has no
  naming convention on purpose: adding one renames existing constraints.

An assertion needing a 395-entry allowlist is not a check, it is a changelog, and
it would become the next ``expected-schemas.tsv`` — a file nobody reads that
silently stops meaning anything.

So this gates on **tables and columns** only: a table or column that exists in
the models but not the database (or vice versa) is something that raises at
runtime. Type and index drift is reported by the script but not gated yet.

Gated behind RUN_SCHEMA_DRIFT_TESTS because it needs a live, migrated database —
the same convention as the other service-dependent suites (see
scripts/run-integration-tests.sh).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIFT_SCRIPT = REPO_ROOT / "scripts" / "check-schema-drift.py"

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SCHEMA_DRIFT_TESTS", "").lower() != "true",
    reason="needs a live migrated database; set RUN_SCHEMA_DRIFT_TESTS=true",
)


def _drift_report() -> dict:
    proc = subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT), "--json"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if proc.returncode == 3:
        pytest.skip(f"database unreachable: {proc.stderr.strip()[:200]}")
    report: dict = json.loads(proc.stdout)
    return report


def test_no_table_level_drift():
    """A model with no table, or a table with no model.

    Known offenders at the time of writing: upload_session, speaker_audio_clip
    and user_certificate_preferences exist in the database with no model and no
    reference anywhere under app/ — migration-created leftovers from removed
    features. They need a migration that drops them, not an allowlist here.
    """
    report = _drift_report()
    count = report["by_category"].get("tables", 0)
    assert count == 0, (
        f"{count} table-level difference(s) between models and schema. "
        "Run ./scripts/check-schema-drift.py for the list."
    )


def test_no_column_level_drift():
    report = _drift_report()
    count = report["by_category"].get("columns", 0)
    assert count == 0, (
        f"{count} column-level difference(s) between models and schema. "
        "Run ./scripts/check-schema-drift.py for the list."
    )


def test_drift_report_is_machine_readable():
    """The release gate consumes this; a schema change must break a test."""
    report = _drift_report()
    assert report["stage"] == "schema-drift"
    assert report["status"] in {"pass", "fail"}
    assert isinstance(report["total_diffs"], int)
    assert isinstance(report["by_category"], dict)
