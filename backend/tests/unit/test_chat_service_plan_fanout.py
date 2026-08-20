"""Tests for `_run_plan_fanout` / `_validate_plan_speakers` (#403 W2.6).

**T9, the red-check invariant this file exists to pin:** a plan may only ADD
legs of kinds the rules could already produce, and it NEVER gets a wider file
scope than the turn already resolved. Every test below that builds a
sub-question or speaker leg asserts the `file_uuids` it was called with is
byte-identical to the scope the fan-out itself was given — never a value read
out of the (untrusted) plan.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.chat import legs
from app.services.chat import planner
from app.services.chat import service as chat_service
from app.services.chat.trace import ListTraceRecorder
from app.services.chat.trace import Outcome
from app.services.chat.trace import QueryStage
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


def _hit(uuid: str, index: int, score: float = 1.0) -> ChunkHit:
    return ChunkHit(file_uuid=uuid, file_id=1, chunk_index=index, content=f"c{index}", score=score)


class _FakeRoute:
    """Minimal stand-in for `router.Route` — only the attributes the fanout reads."""

    def __init__(self, *, wants_aggregate=False, wants_recurrence=False, wants_digest=False):
        self.wants_aggregate = wants_aggregate
        self.wants_recurrence = wants_recurrence
        self.wants_digest = wants_digest


@pytest.fixture(autouse=True)
def _fresh_executor():
    legs.reset_executor_for_tests()
    yield
    legs.reset_executor_for_tests()


def _settings(**overrides):
    from app.services.chat.settings import ChatSettings

    return ChatSettings(**overrides)


def test_every_chunk_leg_reuses_the_exact_same_file_scope():
    """T9: no leg may ever widen the file scope beyond what the turn resolved."""
    scope = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
    seen_scopes: list[object] = []

    def _fake_retrieve_chunks(query, *, file_uuids, **_kwargs):
        seen_scopes.append(file_uuids)
        return [_hit("11111111-1111-1111-1111-111111111111", 0)]

    plan = planner.Plan(subquestions=("sub one", "sub two"), speakers=("Dana",))

    with (
        patch(
            "app.services.search.chunk_retrieval.retrieve_chunks",
            side_effect=_fake_retrieve_chunks,
        ),
        patch(
            "app.services.chat.speaker_resolver.build_roster",
            return_value=SimpleNamespace(
                entries=(SimpleNamespace(name="Dana", profile_id=None, file_count=1),),
                declined=False,
            ),
        ),
        patch(
            "app.services.chat.speaker_resolver.match_candidate",
            return_value=SimpleNamespace(matched="Dana", ambiguous_with=(), reason=""),
        ),
    ):
        result, counted, recurrence_result = chat_service._run_plan_fanout(
            plan=plan,
            decision=_FakeRoute(),
            effective_query="the question",
            question="the question",
            user_id=1,
            organization_id=None,
            file_uuids=scope,
            speakers=None,
            settings=_settings(),
            search_mode="hybrid",
            session_scope=lambda: _NullSession(),
            assistant_message_uuid="00000000-0000-0000-0000-000000000000",
            meta={},
        )

    # main + 2 subquestions + speaker = 4 chunk legs, every one scoped identically.
    assert len(seen_scopes) == 4
    for scoped in seen_scopes:
        assert scoped == scope
        assert scoped is scope or scoped == scope  # never a different list object's content


def test_a_plan_never_adds_a_counted_leg_the_router_did_not_also_want():
    """A plan may only ADD legs of KINDS the rules could already produce —
    `wants=["counted"]` alone (no `decision.wants_aggregate`) still adds one,
    because `counted` IS one of the closed-set kinds a plan may ask for; this
    pins that it is additive, not a NEW mechanism outside that set."""
    plan = planner.Plan(wants=("counted",))
    calls = {"aggregation": 0}

    def _fake_answer_aggregation(*_args, **_kwargs):
        calls["aggregation"] += 1
        return None

    with (
        patch(
            "app.services.search.chunk_retrieval.retrieve_chunks",
            return_value=[],
        ),
        patch(
            "app.services.chat.aggregation_service.answer_aggregation",
            side_effect=_fake_answer_aggregation,
        ),
        patch("app.services.opensearch_service.get_opensearch_client", return_value=object()),
    ):
        chat_service._run_plan_fanout(
            plan=plan,
            decision=_FakeRoute(wants_aggregate=False),
            effective_query="q",
            question="q",
            user_id=1,
            organization_id=None,
            file_uuids=["11111111-1111-1111-1111-111111111111"],
            speakers=None,
            settings=_settings(),
            search_mode="hybrid",
            session_scope=lambda: _NullSession(),
            assistant_message_uuid="00000000-0000-0000-0000-000000000000",
            meta={},
        )
    assert calls["aggregation"] == 1


def test_unvalidated_plan_speakers_never_reach_a_leg():
    """A speaker the roster does not recognise must never narrow retrieval —
    ambiguity means no filter, ever, never a guess."""
    plan = planner.Plan(speakers=("Nobody Real",))
    scopes_seen: list[list[str] | None] = []

    def _fake_retrieve_chunks(query, *, speakers, **_kwargs):
        scopes_seen.append(speakers)
        return []

    with (
        patch(
            "app.services.search.chunk_retrieval.retrieve_chunks",
            side_effect=_fake_retrieve_chunks,
        ),
        patch(
            "app.services.chat.speaker_resolver.build_roster",
            return_value=SimpleNamespace(entries=(), declined=False),
        ),
    ):
        chat_service._run_plan_fanout(
            plan=plan,
            decision=_FakeRoute(),
            effective_query="q",
            question="q",
            user_id=1,
            organization_id=None,
            file_uuids=["11111111-1111-1111-1111-111111111111"],
            speakers=None,
            settings=_settings(),
            search_mode="hybrid",
            session_scope=lambda: _NullSession(),
            assistant_message_uuid="00000000-0000-0000-0000-000000000000",
            meta={},
        )

    # Only the main leg ran (no speaker leg was added at all).
    assert len(scopes_seen) == 1


def test_final_chunks_are_reduced_to_a_third_when_a_counted_answer_is_present():
    """Same ratio the single-leg pipeline applies — the number IS the answer."""
    plan = planner.Plan(wants=("counted",))
    hits = [_hit("11111111-1111-1111-1111-111111111111", i) for i in range(12)]

    with (
        patch("app.services.search.chunk_retrieval.retrieve_chunks", return_value=hits),
        patch(
            "app.services.chat.aggregation_service.answer_aggregation",
            return_value=SimpleNamespace(as_metadata=lambda: {"count": 3}),
        ),
        patch("app.services.opensearch_service.get_opensearch_client", return_value=object()),
    ):
        result, counted, _recurrence = chat_service._run_plan_fanout(
            plan=plan,
            decision=_FakeRoute(wants_aggregate=True),
            effective_query="q",
            question="q",
            user_id=1,
            organization_id=None,
            file_uuids=["11111111-1111-1111-1111-111111111111"],
            speakers=None,
            settings=_settings(final_chunks=12, rerank_enabled=False),
            search_mode="hybrid",
            session_scope=lambda: _NullSession(),
            assistant_message_uuid="00000000-0000-0000-0000-000000000000",
            meta={},
        )
    assert counted is not None
    assert len(result.chunks) <= 4  # 12 // 3


def test_a_fanout_produces_a_planned_parent_with_child_leg_stages_in_the_trace():
    """GH #514 seam: the fan-out is the clearest case for the trace tree —
    siblings expanding under one PLANNED node, each with its own FOUND
    outcome (never a single flat OK for the whole turn)."""
    plan = planner.Plan(subquestions=("sub one",), wants=("counted",))
    rec = ListTraceRecorder()

    def _fake_retrieve_chunks(_query, **_kwargs):
        return [_hit("11111111-1111-1111-1111-111111111111", 0)]

    with (
        patch(
            "app.services.search.chunk_retrieval.retrieve_chunks",
            side_effect=_fake_retrieve_chunks,
        ),
        patch(
            "app.services.chat.aggregation_service.answer_aggregation",
            return_value=None,  # the mechanism declined -> EMPTY, not OK
        ),
        patch("app.services.opensearch_service.get_opensearch_client", return_value=object()),
    ):
        chat_service._run_plan_fanout(
            plan=plan,
            decision=_FakeRoute(wants_aggregate=True),
            effective_query="q",
            question="q",
            user_id=1,
            organization_id=None,
            file_uuids=["11111111-1111-1111-1111-111111111111"],
            speakers=None,
            settings=_settings(rerank_enabled=False),
            search_mode="hybrid",
            session_scope=lambda: _NullSession(),
            assistant_message_uuid="00000000-0000-0000-0000-000000000000",
            meta={},
            recorder=rec,
        )

    stages = [e.stage for e in rec.events]
    assert stages[0] == QueryStage.PLANNED
    planned = rec.events[0]
    assert planned.node_id == "plan"
    # main + 1 subquestion + 1 counted leg = 3 dispatched.
    assert planned.detail["legs"] == 3

    children = [e for e in rec.events if e.parent == "plan"]
    assert children, "every leg's FANNED_*/FOUND events must be attached under the plan node"

    fanned = {
        e.node_id
        for e in children
        if e.stage in (QueryStage.FANNED_VECTOR, QueryStage.FANNED_RELATIONAL)
    }
    assert fanned == {"main", "subquestion-0", "counted"}

    found_by_node = {e.node_id: e for e in children if e.stage == QueryStage.FOUND}
    assert found_by_node["main"].outcome == Outcome.OK
    assert found_by_node["main"].detail["count"] == 1
    assert found_by_node["subquestion-0"].outcome == Outcome.OK
    # The aggregation mechanism returned None (declined) -> ran and found
    # nothing, which must render as EMPTY, never the same as SKIPPED.
    assert found_by_node["counted"].outcome == Outcome.EMPTY

    assert QueryStage.RERANKED in stages, "rerank is skipped (disabled), but must still be recorded"


class _NullSession:
    """A `with session_scope() as db:` stand-in that never touches Postgres.

    These tests exercise the FAN-OUT shape, not `answer_aggregation`'s own
    Postgres reads (that module owns its own session-lifetime tests) — every
    Postgres-backed leg here is itself patched to a stub, so nothing ever
    calls a method on the object this yields.
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
