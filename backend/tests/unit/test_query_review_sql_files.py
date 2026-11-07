"""Every replayable query-review SQL file must plan against the real schema (issue #1337).

``scripts/perf/query-review/queries/*.sql`` are replayed by ``explain.sh`` on a perf stack hours
into a run; a column typo found there wastes the run. Plain ``EXPLAIN`` (no ANALYZE) parses and
plans each one on the shared test DB, so a schema rename breaks this test instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

QUERIES = Path(__file__).resolve().parents[3] / "scripts" / "perf" / "query-review" / "queries"
FILES = sorted(QUERIES.glob("*.sql"))


def test_the_expected_controls_and_hypotheses_are_present():
    names = {p.stem for p in FILES}

    assert {"C1", "C2", "C3", "G7", "G13", "G13.after", "P3", "S1", "T4", "V7", "P8", "P9"} <= names


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_query_plans_against_the_current_schema(db_session, path):
    body = (
        "\n".join(line.split("--")[0] for line in path.read_text().splitlines()).strip().rstrip(";")
    )

    plan = "\n".join(str(r[0]) for r in db_session.execute(text(f"EXPLAIN {body}")))

    assert "(cost=" in plan, plan
