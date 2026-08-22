"""LLM query planner (#403 W2.6): turn-1 trigger, plan schema, and the LLM calls.

**Never a routing-only call (#403's D6 posture, extended).** A follow-up turn
never reaches :func:`needs_plan` at all — it already pays for a rewrite call
whenever ``chat.rag.query_rewrite_enabled`` and history exist, so a plan for
turn 2+ is a THIRD line appended to that same response (see
``query_rewriter.rewrite_query``'s ``want_plan`` argument), never a second
round trip. Turn 1 has no rewrite call to piggyback on, so it is the only case
where a plan costs a standalone LLM call — and :func:`needs_plan` exists
precisely to keep that call rare: it must fire on well under 15% of ordinary
lookup questions, or the feature spends a call on every ordinary turn a
rules-only router already answers for free.

The plan itself is untrusted model output and is treated that way end to end:
strict single-line JSON, a key allowlist, bounded list lengths, and — the
caller's job, not this module's — every planner-supplied name is
RE-VALIDATED through the speaker roster before it can add a retrieval leg.
Malformed output degrades to :data:`FAILED_PLAN`, never a partial parse.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# --- the plan schema --------------------------------------------------------

MAX_SUBQUESTIONS = 4
MAX_SUBQUESTION_CHARS = 200
MAX_SPEAKERS = 5

_ALLOWED_KEYS = frozenset({"subquestions", "speakers", "time", "wants"})
#: Leg *kinds* a plan may ask for. Deliberately a closed set matching exactly
#: what the rules-only pipeline can already produce (counted/recurrence tiers,
#: the digest map, a speaker-scoped leg) — a plan may only ever ADD one of
#: these, never invent a new retrieval mechanism.
_ALLOWED_WANTS = frozenset({"counted", "recurrence", "digest", "speaker"})


@dataclass(frozen=True)
class Plan:
    """A validated plan, or the sentinel :data:`FAILED_PLAN`.

    Every field is already bounded and type-checked by :func:`_parse_plan` —
    nothing downstream needs to re-validate SHAPE. What downstream code (the
    caller, ``legs.py``) MUST still re-validate is MEANING: ``speakers`` are
    free-text model output and are only usable once matched against the real
    roster, and every leg built from this plan must stay inside the scope the
    turn already resolved (never a new file scope, never SQL, never a changed
    router decision).
    """

    subquestions: tuple[str, ...] = ()
    speakers: tuple[str, ...] = ()
    time: dict | None = None
    wants: tuple[str, ...] = ()
    failed: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.subquestions or self.speakers or self.time or self.wants)

    def as_metadata(self) -> dict:
        """Shape for ``msg_metadata.plan`` — matches the frontend's ``{steps}`` contract.

        ``ChatMessageMeta.svelte`` renders ``meta.plan.steps.join(' -> ')`` and
        nothing else, so the full structured plan is summarized into one
        ordered list of short strings rather than exposed verbatim — a
        diagnostics panel, not a debugger. ``{"failed": True}`` for the
        sentinel (no ``steps`` key, matching the pre-existing shape the
        component already guards with ``meta.plan?.steps?.length``).
        """
        if self.failed:
            return {"failed": True}
        if self.is_empty:
            return {}
        steps = list(self.subquestions) or [f"want:{w}" for w in self.wants]
        if self.speakers:
            steps = steps + [f"speaker:{name}" for name in self.speakers]
        return {"steps": steps}


#: The single sentinel for "a plan was attempted and failed" — never a partial
#: `Plan` with some fields populated and others defaulted, which would read as
#: a real (if sparse) plan to a caller that only checks `is_empty`.
FAILED_PLAN = Plan(failed=True)


def _parse_plan(raw: str) -> Plan | None:
    """Strict single-line JSON parse. ``None`` on ANY malformity — never partial.

    A model that wraps its answer in prose, emits multiple lines, or invents
    an extra key produces ``None`` here, and the caller's contract is that
    ``None`` means "route by rules" — there is no partial-credit path that
    would let a plan with an unrecognised key still add a leg.
    """
    if not raw:
        return None
    line = raw.strip().splitlines()[0].strip()
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not set(data.keys()) <= _ALLOWED_KEYS:
        return None

    subq_raw = data.get("subquestions", [])
    if not isinstance(subq_raw, list) or len(subq_raw) > MAX_SUBQUESTIONS:
        return None
    subquestions: list[str] = []
    for item in subq_raw:
        if not isinstance(item, str) or not item.strip():
            return None
        subquestions.append(item.strip()[:MAX_SUBQUESTION_CHARS])

    speakers_raw = data.get("speakers", [])
    if not isinstance(speakers_raw, list) or len(speakers_raw) > MAX_SPEAKERS:
        return None
    speakers = [s.strip() for s in speakers_raw if isinstance(s, str) and s.strip()]

    time_raw = data.get("time", {}) or {}
    if not isinstance(time_raw, dict):
        return None

    wants_raw = data.get("wants", [])
    if not isinstance(wants_raw, list):
        return None
    wants = [w for w in wants_raw if isinstance(w, str) and w in _ALLOWED_WANTS]

    return Plan(
        subquestions=tuple(subquestions),
        speakers=tuple(speakers),
        time=time_raw or None,
        wants=tuple(wants),
    )


def parse_plan_line(raw: str) -> Plan | None:
    """Read a ``PLAN: {json}`` third line out of a rewrite response.

    Mirrors ``router.parse_intent_line``'s contract exactly: anything
    unrecognised (no such line, malformed JSON, an extra key) returns
    ``None`` and costs the caller nothing beyond the rewrite call it was
    already making.

    Args:
        raw: The rewrite response's full text (rewritten query on line 1,
            optional ``INTENT:`` on line 2, optional ``PLAN:`` on line 3).

    Returns:
        A :class:`Plan`, or ``None``.
    """
    for line in (raw or "").splitlines()[1:5]:
        stripped = line.strip()
        if not stripped.upper().startswith("PLAN:"):
            continue
        return _parse_plan(stripped.split(":", 1)[1].strip())
    return None


# --- the turn-1 trigger ------------------------------------------------------

#: A `?`-terminated clause opening on an interrogative word. Deliberately
#: coarse — this only has to distinguish "one question" from "several", not
#: parse grammar, and the fire-rate gate (<=15% on lookups) is what keeps a
#: coarse detector honest.
_INTERROGATIVE_RE = re.compile(
    r"\b(?:what|when|where|who|why|how|which|did|does|do|is|are|was|were|"
    r"can|could|should|would|will)\b[^?.!]*\?",
    re.IGNORECASE,
)

_ENUMERATION_RE = re.compile(
    r"(?:^|\s)(?:\d+[).]|\bfirst\b|\bsecond\b|\bthird\b|\bthen\b|\bfinally\b|"
    r"\balso\b|\bas well as\b|\band additionally\b)",
    re.IGNORECASE,
)

#: A capitalized name followed (within a short window) by a speech/opinion
#: verb — "what did Dana say", "does Ravi think". Two or more DISTINCT names
#: in this shape is the "two speaker-verb frames" signal from the brief.
_SPEAKER_VERB_RE = re.compile(
    r"\b([A-Z][a-zA-Z]+)\b[^.?!]{0,40}\b"
    r"(?:said|says?|say|think|thinks|thought|mentioned|asked|told|felt|feels?|believes?)\b"
)

_COMPARISON_RE = re.compile(
    r"\b(?:compare|compared to|versus|vs\.?|difference between|"
    r"across (?:meetings|recordings|calls|sessions|conversations))\b",
    re.IGNORECASE,
)


def _count_interrogatives(text: str) -> int:
    return len(_INTERROGATIVE_RE.findall(text))


def _speaker_verb_frame_count(text: str) -> int:
    return len({m.group(1) for m in _SPEAKER_VERB_RE.finditer(text)})


def _is_multi_part(text: str) -> bool:
    """>=2 interrogative clauses, an enumeration marker, or 2 distinct speaker-verb frames."""
    if _count_interrogatives(text) >= 2:
        return True
    if _ENUMERATION_RE.search(text):
        return True
    return _speaker_verb_frame_count(text) >= 2


def needs_plan(
    *,
    question: str,
    route,
    ambiguous_speaker: bool = False,
    non_english_locale: bool = False,
) -> bool:
    """Pure decision: does turn 1 deserve a standalone planner call?

    **Zero latency for an ordinary lookup** — every check here is a compiled
    regex over the question text and a read of the already-computed
    :class:`~app.services.chat.router.Route`; nothing loads a model or makes a
    call. Fires on:

    - recurrence intent (``route.wants_recurrence``) — the router already
      detected a cross-meeting question and a plan is what turns that into
      the parallel per-topic legs the shape actually needs;
    - an ambiguous speaker resolution — the roster already found more than
      one candidate and a plan is one way to let the model pick a
      disambiguating sub-query rather than the turn simply declining;
    - multi-part structure — two-or-more interrogative clauses, an
      enumeration, or two distinct speaker-verb frames;
    - a cross-meeting comparison marker ("compare", "versus", …);
    - rules-found-nothing (``not route.signals``) on an otherwise-plain
      lookup, PLUS a non-English locale/script — this is deliberately also
      the multilingual turn-1 gap: turn 2+ already gets a routing hint for
      free from the rewrite call's ``INTENT:`` line, so turn 1 is the only
      case a non-English lookup has no signal at all to route on.

    Args:
        question: The user's message, as typed (never the rewrite — this is
            turn 1, which by definition has no rewrite).
        route: The router's already-computed :class:`Route` for this
            question (cost: microseconds, already paid regardless of the
            planner).
        ambiguous_speaker: Whether ``chat.speaker_resolver`` found more than
            one roster match for a name mentioned in the question.
        non_english_locale: Whether the question's script/locale is
            non-English (a language detector call site's job to supply —
            this function makes no I/O of its own).

    Returns:
        ``True`` when a standalone planner call is worth making.
    """
    clean = " ".join(str(question or "").split())
    if not clean:
        return False
    if route.wants_recurrence:
        return True
    if ambiguous_speaker:
        return True
    if _is_multi_part(clean):
        return True
    if _COMPARISON_RE.search(clean):
        return True
    return bool(not route.signals and route.intent == "lookup" and non_english_locale)


# --- the LLM calls -----------------------------------------------------------

_PLANNER_SYSTEM = (
    "You are a query planner for a transcript search assistant. Decide whether "
    "the user's question should be split into independent parts before "
    "searching.\n"
    "Reply with EXACTLY ONE LINE of JSON and nothing else — no prose, no code "
    "fence — matching this shape:\n"
    '{"subquestions": ["..."], "speakers": ["..."], "time": {}, "wants": []}\n'
    "subquestions: up to 4 independent search queries this question can be "
    "split into (200 characters max each, in the question's own language), "
    "or [] if it should not be split.\n"
    "speakers: up to 5 person names mentioned by the user, or [].\n"
    'time: {"year": <int>, "month": <int>} if a specific date is mentioned, '
    "else {}.\n"
    'wants: any of "counted", "recurrence", "digest", "speaker" that apply, or [].\n'
    "Reply with nothing except that one line of JSON."
)

_PLANNER_MAX_TOKENS = 100


def build_plan(llm, question: str, *, max_tokens: int = _PLANNER_MAX_TOKENS) -> tuple[Plan, int]:
    """Turn-1 standalone planner call.

    Only reached when :func:`needs_plan` fired — never a routing-only call
    otherwise. Every failure mode (no LLM, a provider error, malformed
    output) returns :data:`FAILED_PLAN`, matching how every other optional
    LLM enhancement in this package degrades: an enhancement in the hot path
    is never a dependency.

    Args:
        llm: The caller's ``LLMService``, or ``None``.
        question: The user's message, as typed.
        max_tokens: Reply budget for the planner call.

    Returns:
        ``(plan, llm_calls)`` — ``llm_calls`` is ``0`` when no call was made
        at all (``llm`` was ``None``) and ``1`` otherwise, so the caller can
        meter it exactly regardless of whether the call succeeded.
    """
    if llm is None:
        return FAILED_PLAN, 0

    messages = [
        {"role": "system", "content": _PLANNER_SYSTEM},
        # Concatenation only — the question is untrusted text.
        {"role": "user", "content": question},
    ]
    try:
        response = llm.chat_completion(messages, max_tokens=max_tokens, temperature=0)
    except Exception as exc:  # noqa: BLE001 — an enhancement, never a dependency
        logger.info(f"Chat planner call failed, routing by rules: {exc}")
        return FAILED_PLAN, 1

    raw = getattr(response, "content", "") or ""
    plan = _parse_plan(raw)
    if plan is None:
        logger.info("Chat planner returned unusable output, routing by rules")
        return FAILED_PLAN, 1
    return plan, 1


# --- the follow-up extension of the rewrite system prompt -------------------

#: Appended to `query_rewriter._REWRITE_SYSTEM` when the caller wants a plan
#: too. A pure text fragment — `query_rewriter.py` owns the instruction to
#: send `PLAN:` as a THIRD line and how many tokens to allow for it, this
#: module only owns what the third line must contain.
PLAN_LINE_INSTRUCTION = (
    "On a THIRD line, write 'PLAN: ' followed by exactly one line of JSON "
    'matching {"subquestions": ["..."], "speakers": ["..."], "time": {}, '
    '"wants": []} — subquestions up to 4 independent search queries (200 '
    "chars max each) this question can be split into, or [] if it should not "
    'be split; speakers up to 5 names mentioned, or []; time {"year": <int>, '
    '"month": <int>} if a date is mentioned, else {}; wants any of "counted", '
    '"recurrence", "digest", "speaker" that apply, or []. If nothing applies, '
    'write \'PLAN: {"subquestions": [], "speakers": [], "time": {}, "wants": []}\'.'
)
