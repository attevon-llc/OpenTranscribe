"""Tests for the LLM query planner (#403 W2.6): trigger, schema, calls.

Nothing here needs Postgres/Redis/OpenSearch — the planner module loads
nothing and calls nothing except the LLM stub the tests provide.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.chat import planner
from app.services.chat.router import Route
from app.services.chat.router import route

pytestmark = pytest.mark.unit


def _lookup_route(question: str) -> Route:
    return route(question)


# --------------------------------------------------------------------------- schema / parsing


def test_parse_plan_line_reads_a_well_formed_third_line():
    raw = (
        "the rewritten query\n"
        "INTENT: lookup\n"
        'PLAN: {"subquestions": ["a", "b"], "speakers": ["Dana"], '
        '"time": {"year": 2025}, "wants": ["counted"]}\n'
    )
    plan = planner.parse_plan_line(raw)
    assert plan is not None
    assert plan.subquestions == ("a", "b")
    assert plan.speakers == ("Dana",)
    assert plan.time == {"year": 2025}
    assert plan.wants == ("counted",)
    assert not plan.failed


def test_parse_plan_line_returns_none_with_no_plan_line():
    assert planner.parse_plan_line("rewritten query\nINTENT: lookup\n") is None


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "{not even valid json",
        '{"subquestions": "not a list"}',
        '{"subquestions": ["1", "2", "3", "4", "5"]}',  # over MAX_SUBQUESTIONS
        '{"speakers": ["a", "b", "c", "d", "e", "f"]}',  # over MAX_SPEAKERS
        '{"unexpected_key": 1}',
        '{"subquestions": [1, 2]}',  # not strings
        '{"time": "not a dict"}',
        '["a", "list", "not", "a", "dict"]',
    ],
)
def test_malformed_plan_json_fails_closed(raw):
    """T3: strict single-line parse. ANY malformity -> None, never a partial parse."""
    assert planner._parse_plan(raw) is None


def test_a_plan_with_only_some_fields_is_still_valid():
    """Every key is optional; a plan naming only `wants` is legitimate."""
    plan = planner._parse_plan('{"wants": ["recurrence"]}')
    assert plan is not None
    assert plan.wants == ("recurrence",)
    assert plan.subquestions == ()
    assert plan.speakers == ()


def test_plan_as_metadata_shapes_the_frontend_contract():
    """`ChatMessageMeta.svelte` renders `meta.plan.steps` and nothing else."""
    plan = planner.Plan(subquestions=("a", "b"), speakers=("Dana",))
    meta = plan.as_metadata()
    assert meta["steps"] == ["a", "b", "speaker:Dana"]


def test_failed_plan_metadata_has_no_steps():
    assert planner.FAILED_PLAN.as_metadata() == {"failed": True}
    assert planner.FAILED_PLAN.failed


def test_empty_plan_metadata_is_empty():
    assert planner.Plan().as_metadata() == {}
    assert planner.Plan().is_empty


# --------------------------------------------------------------------------- the LLM calls


@dataclass
class _Response:
    content: str


class _StubLLM:
    def __init__(self, content: str | None = None, raise_exc: Exception | None = None):
        self.content = content
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def chat_completion(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self.raise_exc:
            raise self.raise_exc
        return _Response(self.content or "")


def test_build_plan_with_no_llm_makes_no_call_and_fails_closed():
    plan, calls = planner.build_plan(None, "what did Dana say and what did Ravi say?")
    assert plan is planner.FAILED_PLAN
    assert calls == 0


def test_build_plan_parses_a_well_formed_reply():
    llm = _StubLLM(content='{"subquestions": ["x"], "speakers": [], "time": {}, "wants": []}')
    plan, calls = planner.build_plan(llm, "some question")
    assert calls == 1
    assert not plan.failed
    assert plan.subquestions == ("x",)
    assert llm.calls[0]["max_tokens"] == planner._PLANNER_MAX_TOKENS
    assert llm.calls[0]["temperature"] == 0


def test_build_plan_degrades_on_malformed_output():
    llm = _StubLLM(content="not json")
    plan, calls = planner.build_plan(llm, "some question")
    assert plan.failed
    assert calls == 1


def test_build_plan_degrades_on_provider_error():
    llm = _StubLLM(raise_exc=RuntimeError("boom"))
    plan, calls = planner.build_plan(llm, "some question")
    assert plan.failed
    assert calls == 1


# --------------------------------------------------------------------------- the turn-1 trigger


def test_needs_plan_fires_on_recurrence_intent():
    r = route("what keeps coming up across our meetings?", recurrence_enabled=True)
    assert r.wants_recurrence
    assert planner.needs_plan(question="what keeps coming up across our meetings?", route=r)


def test_needs_plan_fires_on_ambiguous_speaker():
    r = _lookup_route("what did Dana say about pricing")
    assert planner.needs_plan(
        question="what did Dana say about pricing", route=r, ambiguous_speaker=True
    )


@pytest.mark.parametrize(
    "question",
    [
        "What did Dana say about pricing? When did Ravi bring up the budget?",
        "First, summarize the kickoff. Then, list the action items.",
        "What did Dana say about pricing and what did Ravi think about the timeline?",
    ],
)
def test_needs_plan_fires_on_multi_part_structure(question):
    r = _lookup_route(question)
    assert planner.needs_plan(question=question, route=r)


def test_needs_plan_fires_on_comparison_markers():
    q = "Compare what was said about pricing across the March and April meetings"
    r = _lookup_route(q)
    assert planner.needs_plan(question=q, route=r)


def test_needs_plan_fires_on_rules_found_nothing_plus_non_english():
    q = "cual fue la decision sobre el presupuesto"
    r = _lookup_route(q)
    assert not r.signals
    assert planner.needs_plan(question=q, route=r, non_english_locale=True)


def test_needs_plan_does_not_fire_on_the_same_question_without_the_locale_signal():
    q = "cual fue la decision sobre el presupuesto"
    r = _lookup_route(q)
    assert not planner.needs_plan(question=q, route=r, non_english_locale=False)


# --------------------------------------------------------------------------- T1: fire-rate gate

#: A synthetic corpus of ordinary, single-part, English lookup questions —
#: the class `needs_plan` must almost never fire on. Modelled on the kinds of
#: turns `router.py`'s own docstrings cite as the un-ambiguous lookup case
#: ("what did X say about Y"), deliberately varied in subject and phrasing so
#: the measurement is not an artefact of one template.
_LOOKUP_SUBJECTS = (
    "the budget",
    "the migration timeline",
    "the vendor contract",
    "the Q3 roadmap",
    "the hiring plan",
    "the outage postmortem",
    "the marketing launch",
    "the security review",
    "the customer escalation",
    "the pricing model",
)
_LOOKUP_TEMPLATES = (
    "What did we decide about {subject}?",
    "What was said about {subject} in the meeting?",
    "Can you tell me what happened with {subject}?",
    "What is the status of {subject}?",
    "Who is responsible for {subject}?",
    "What was the outcome of {subject}?",
    "Summarize what was said about {subject}.",
    "When was {subject} last discussed?",
    "What concerns were raised about {subject}?",
    "What is {subject}?",
)

LOOKUP_CORPUS: tuple[str, ...] = tuple(
    template.format(subject=subject)
    for template in _LOOKUP_TEMPLATES
    for subject in _LOOKUP_SUBJECTS
)


def _fires(question: str) -> bool:
    r = _lookup_route(question)
    return planner.needs_plan(question=question, route=r)


def test_planner_fire_rate_on_lookup_corpus_is_under_the_15_percent_gate():
    """T1: >15% on lookups means the trigger is misdesigned — a gate, not a note.

    Also covers the "regenerate/edit take the follow-up arm" requirement:
    those turns never call `needs_plan` at all (they extend the rewrite call
    instead — see `query_rewriter.rewrite_query`'s `want_plan`), so they
    contribute a fixed 0% to a turn-1-only measurement by construction. This
    corpus is intentionally turn-1 shaped (no history) for exactly that
    reason; a real deployment's follow-up traffic is measured by whether the
    rewrite call fires (already gated by `chat.rag.query_rewrite_enabled` and
    history), not by this function.
    """
    fired = sum(1 for q in LOOKUP_CORPUS if _fires(q))
    rate = fired / len(LOOKUP_CORPUS)
    assert rate <= 0.15, (
        f"needs_plan() fired on {fired}/{len(LOOKUP_CORPUS)} ({rate:.1%}) of ordinary "
        "lookup questions — over the 15% gate. This is a design defect, not a tuning nit: "
        "every fire on a lookup is a wasted standalone LLM call turn 1 was supposed to avoid."
    )
