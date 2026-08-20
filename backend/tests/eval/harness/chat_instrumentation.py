"""Per-turn instrumentation extractors, read from a chat turn's ``meta`` dict (#461 W2.E1).

Every function here is pure: given the ``meta`` dict a chat turn's response metadata
carries (what ``chat/service.py`` builds and merges — ``meta["route"]``,
``meta["overview"]``, ``meta["aggregation"]``, etc. — see
``backend/app/services/chat/CLAUDE.md``), extract one instrumentation value or report
that it was not measured.

⚠️ **Absent is not zero, and this module never conflates them.** An extractor that
defaulted a missing key to 0 would read "not measured" as "measured and found zero" —
the confusion this repo's CLAUDE.md calls out by name ("That distinction has burned
this repo before"). Every extractor below returns ``None`` when its source key is
missing, and a value only when it is genuinely present.

Verified against the current codebase while this module was written (all three,
2026-08-19):

* ``llm_calls`` **is emitted today**, but only conditionally.
  ``chat/mapreduce.Overview.as_metadata()`` includes ``"llm_calls"`` and
  ``chat/service.py`` merges it as ``meta["overview"]["llm_calls"]`` — but only on
  turns where the digest map-reduce (the ``<overview>`` block) actually ran. A
  ``lookup``-routed turn with no overview carries no ``"overview"`` key at all, so
  :func:`extract_llm_calls` returns ``None`` for it, correctly distinguishing "this
  turn made no LLM calls in the overview stage" (impossible to know — it never ran)
  from "this turn's overview made 0 calls" (a real, measured zero).
* ``router_language_unmatched`` **has no emitter anywhere in the codebase.**
  ``schemas/chat.ChatWarningCode.ROUTER_LANGUAGE_UNMATCHED`` exists as a warning code
  the SSE contract already reserves, and its own docstring says "(Wave 2; no emitter
  yet)" — grepped, confirmed, nothing sets it. :func:`extract_router_language_unmatched`
  therefore always returns ``None`` today; it exists so the OTHER lane building the
  emitter has one place to land the key name, and so this harness's non-English
  trigger-coverage measure activates the moment that lane ships without this module
  needing to change.
* **Planner fire-rate has no emitter AND no established key name.** Unlike the other
  two, nothing in ``schemas/chat.py`` or ``services/chat/`` names a "planner" concept
  at all — grepped across both, zero hits. :func:`extract_planner_fired` reads a
  **guessed** key (``meta["planner"]["fired"]``), chosen to match the existing
  per-stage-block convention (``meta["route"]``, ``meta["aggregation"]``,
  ``meta["overview"]``) but not confirmed against any other lane's plan. Treat this
  key name as a proposal, not a contract, until the owning lane confirms or changes it.
* ``scope_coverage`` (issue #63) **is emitted today, conditionally**, same shape as
  ``llm_calls``. ``mapreduce.Overview.as_metadata()`` publishes ``files_total`` and
  (since #63) ``files_in_scope``; ``chat/service.py`` sets the sibling top-level keys
  ``meta["map_files_without_artifacts"]`` / ``meta["map_files_no_content"]`` beside
  ``meta["overview"]``, both only when nonzero — the same "counted, not defaulted"
  convention the rest of this module follows. :func:`extract_scope_coverage` combines
  all four into one ratio; it is the harness-side half of
  ``app.services.chat.mapreduce.coverage.check_scope_coverage``, which is the
  production/test-side half that has the ACTUAL file uuids to work with. This module
  only ever sees the aggregated counts a turn's persisted ``meta`` carries — the same
  reason ``llm_calls`` above cannot see content, only what got counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def extract_router_language_unmatched(meta: dict[str, Any]) -> bool | None:
    """Whether the router's detected-language check failed to match, or ``None`` if
    unmeasured (no emitter exists yet — see module docstring).

    Reads ``meta["route"]["language_unmatched"]`` — the natural home given
    ``meta["route"] = decision.as_metadata()`` already carries the router's other
    per-turn facts (``chat/service.py``). Returns ``None`` whenever that key is
    absent, which is every turn today.
    """
    route = meta.get("route")
    if not isinstance(route, dict) or "language_unmatched" not in route:
        return None
    return bool(route["language_unmatched"])


def extract_planner_fired(meta: dict[str, Any]) -> bool | None:
    """Whether a query plan was built and executed for this turn, or ``None`` if
    unmeasured (no emitter AND no established key name yet — see module docstring).

    Reads a PROPOSED ``meta["planner"]["fired"]`` key. No other code in this
    repository currently sets it.
    """
    planner = meta.get("planner")
    if not isinstance(planner, dict) or "fired" not in planner:
        return None
    return bool(planner["fired"])


def extract_llm_calls(meta: dict[str, Any]) -> int | None:
    """LLM calls made by the digest map-reduce stage this turn, or ``None`` if the
    overview stage did not run (see module docstring for why that is not a zero).

    Reads ``meta["overview"]["llm_calls"]``, which IS emitted today by
    ``chat/mapreduce.Overview.as_metadata()`` — but only on turns whose route adds an
    ``<overview>`` block. This does not (yet) cover LLM calls made by other stages
    (the rewrite call, the router's optional ``INTENT:`` line, the final answer
    generation itself) — it is scoped to what ``Overview.as_metadata()`` publishes
    today, not a claim about the turn's total LLM call count.
    """
    overview = meta.get("overview")
    if not isinstance(overview, dict) or "llm_calls" not in overview:
        return None
    return int(overview["llm_calls"])


def extract_scope_coverage(meta: dict[str, Any]) -> float | None:
    """This turn's scope-coverage ratio (issue #63), or ``None`` if unmeasured.

    ``1.0`` means the digest map-reduce accounted for every file in the resolved
    scope — either represented in the ``<overview>`` block or named by a reason
    (``files_without_artifacts``/``files_no_content``). Below ``1.0`` is the
    "recordings: 8 of 25, and nothing said so" shape this measure exists to catch:
    some file the scope named produced neither a hit nor a counted reason.

    Reads ``meta["overview"]["files_total"]`` / ``["files_in_scope"]`` — both
    published by ``mapreduce.Overview.as_metadata()`` whenever the overview stage
    ran at all — plus the top-level ``meta["map_files_without_artifacts"]`` /
    ``meta["map_files_no_content"]`` gap counters ``chat/service.py`` sets beside
    it, defaulting an ABSENT counter to 0 (this module's convention differs from
    ``chat/service.py``'s own "only set when nonzero": here, "the overview ran and
    the key is missing" already means the gap was zero, which is what
    ``chat/service.py``'s emission rule guarantees, not an unmeasured value).

    Returns ``None`` when ``meta["overview"]`` never ran at all (a ``lookup``-routed
    turn, or an unbounded scope with no bounded map to measure — the same case
    ``mapreduce.coverage.check_scope_coverage`` reports as ``applicable=False``) or
    when the published scope size is zero/absent, which is indistinguishable
    between "unbounded" and "a genuinely empty scope" from ``meta`` alone.

    This is the RATIO of accounted files to scope size, not the same thing as
    ``mapreduce.coverage.ScopeCoverage.complete`` — that field is exact-equality
    over real uuids the production/test side has; this is a coarser measure over
    the counts a persisted turn happens to carry, for corpus-wide reporting.
    """
    overview = meta.get("overview")
    if not isinstance(overview, dict):
        return None
    files_in_scope = overview.get("files_in_scope")
    if not files_in_scope:
        return None
    accounted = int(overview.get("files_total") or 0)
    accounted += int(meta.get("map_files_without_artifacts") or 0)
    accounted += int(meta.get("map_files_no_content") or 0)
    return min(1.0, accounted / float(files_in_scope))


@dataclass(frozen=True)
class TurnInstrumentation:
    """One turn's extracted instrumentation, each field independently absent/None."""

    router_language_unmatched: bool | None
    planner_fired: bool | None
    llm_calls: int | None
    scope_coverage: float | None

    def as_json(self) -> dict[str, Any]:
        """JSON-safe form. Keys are always present; values are ``null`` when
        unmeasured, never a default that could be misread as a measurement."""
        return {
            "router_language_unmatched": self.router_language_unmatched,
            "planner_fired": self.planner_fired,
            "llm_calls": self.llm_calls,
            "scope_coverage": self.scope_coverage,
        }


def extract_turn_instrumentation(meta: dict[str, Any]) -> TurnInstrumentation:
    """Run every extractor in this module over one turn's ``meta`` dict."""
    return TurnInstrumentation(
        router_language_unmatched=extract_router_language_unmatched(meta),
        planner_fired=extract_planner_fired(meta),
        llm_calls=extract_llm_calls(meta),
        scope_coverage=extract_scope_coverage(meta),
    )


def summarize_instrumentation(rows: list[TurnInstrumentation]) -> dict[str, Any]:
    """Corpus-level rollup: coverage (how many turns measured each field) and, only
    over the MEASURED subset, the rate/mean.

    A field with zero measured turns reports ``"coverage": 0`` and no
    ``rate``/``mean`` key at all — an absent rate is not silently rendered as 0.0.

    Args:
        rows: One :class:`TurnInstrumentation` per scored turn.

    Returns:
        A dict keyed by field name, each an object with ``coverage`` (measured /
        total) and, when coverage is nonzero, ``rate`` (booleans) or ``mean``
        (``llm_calls``).
    """
    total = len(rows)
    out: dict[str, Any] = {}

    for field_name in ("router_language_unmatched", "planner_fired"):
        measured = [
            getattr(row, field_name) for row in rows if getattr(row, field_name) is not None
        ]
        entry: dict[str, Any] = {"coverage": len(measured), "total": total}
        if measured:
            entry["rate"] = sum(1 for value in measured if value) / len(measured)
        out[field_name] = entry

    llm_calls = [row.llm_calls for row in rows if row.llm_calls is not None]
    entry = {"coverage": len(llm_calls), "total": total}
    if llm_calls:
        entry["mean"] = sum(llm_calls) / len(llm_calls)
    out["llm_calls"] = entry

    scope_coverage = [row.scope_coverage for row in rows if row.scope_coverage is not None]
    entry = {"coverage": len(scope_coverage), "total": total}
    if scope_coverage:
        entry["mean"] = sum(scope_coverage) / len(scope_coverage)
        # The gate this measure exists for: not "coverage is usually high" but
        # "coverage is NEVER silently incomplete" — a mean of 0.98 could still
        # be one turn at 0.0, and averaging would hide exactly that turn.
        entry["min"] = min(scope_coverage)
    out["scope_coverage"] = entry

    return out
