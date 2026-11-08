"""The held filter and ``held_count`` must plan as a semi-join, never a per-row SubPlan (#1337 G8).

``EXISTS(...)`` under ``OR legal_hold`` cannot be pulled up into a join, so inside the
``/files/metadata-filters`` aggregate it ran once per scoped row (19,560 loops and 344,680
sequential scans of ``content_hold`` at 100k files). The plan assertion pins node types only, never
costs, so it holds on a tiny CI database. Semantics (both authorities, released holds, tenant
scope) are covered by ``test_gallery_held_filter.py``.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import text

from app.models.media import MediaFile
from app.services.hold_guard import held_file_ids
from tests.api.test_gallery_held_filter import FACETS
from tests.api.test_gallery_held_filter import _held_count
from tests.api.test_gallery_held_filter import _hold
from tests.api.test_gallery_held_filter import _platform_hold
from tests.query_budget import count_statements


def test_held_file_ids_plans_as_a_semi_join_without_a_subplan(gov):
    query = select(func.count(MediaFile.id)).where(MediaFile.id.in_(held_file_ids()))
    compiled = query.compile(
        dialect=gov.db.get_bind().dialect, compile_kwargs={"literal_binds": True}
    )

    plan = "\n".join(r[0] for r in gov.db.execute(text(f"EXPLAIN {compiled}")))

    # A semi-join, or a join against the de-duplicated union (the planner's choice of the two).
    assert "Join" in plan, plan
    assert "SubPlan" not in plan, plan


def test_held_count_statement_has_no_exists_inside_the_aggregate(client, gov):
    org_held, platform_held, free = (gov.file(gov.alice) for _ in range(3))
    _hold(gov, org_held)
    _platform_hold(gov, platform_held)

    with gov.acting_as(gov.alice):
        with count_statements(gov.db.connection()) as log:
            assert _held_count(client) == 2, "control: both authorities counted, the free file not"

    assert log.matching(r"count\(.*\)\s+FILTER.*content_hold") == [], log.describe()
    assert free.id is not None and FACETS
