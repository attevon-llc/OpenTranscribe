"""Reconcile a resolved scope against what the map step actually drew on (issue #63).

The bug this exists to catch, in the CLAUDE.md's own words: asked for a summary over a
25-file scope, ``retrieve_digests`` (the RANKED digest leg) returned 50 sections drawn
from **8 files**, because sections cluster by relevance. The composed block was headed
``recordings: 8`` and the model answered confidently over a scope of 25. No eval
framework catches this — RAGAS/DeepEval/ARES score groundedness against whatever
context was retrieved, and every claim in that answer genuinely WAS grounded in the 8
files it saw. The only thing that catches it is a count: ``files_touched ==
files_in_scope``, or a NAMED, counted reason for every file that fell short.

**This module does not fix the ranking-vs-mapping bug** — ``file_summaries.scope_digest_hits``
already fixed that, by reading ``file_facts`` for every file in a bounded scope directly
instead of ranking (see that module's own docstring). What this module adds is the
reconciliation step nothing previously performed: given the resolved scope and the map's
output (hits plus its own ``coverage`` dict), does every scoped file appear either as a
hit or as a **counted, named** reason it does not? A caller that skips this check can
still silently regress to the ranked leg — a mocked-out map, a future code path that
forgets to call ``scope_digest_hits`` at all — and nothing would notice.

## Two gaps a raw ``len(hits)`` comparison cannot separate

A file that produced no hit is either:

* **never consulted** — no ``file_facts`` row exists yet (``coverage["files_without_artifacts"]``),
  or the uuid matched no table at all; or
* **consulted, contributed nothing** — a real digest exists but its ``sections`` list is
  empty, e.g. a near-silent recording (``coverage["files_no_content"]``).

Conflating the two is exactly the failure mode this task exists to prevent from moving
one level down: a legitimate zero-content file would otherwise look identical, from the
map's output alone, to a file that fell out of the map by accident. Both are named,
counted keys on the SAME ``coverage`` dict :func:`~file_summaries.scope_digest_hits`
already returns — this module reads them, it does not invent a third bookkeeping path.

## ``file_uuids=None`` vs ``file_uuids=[]`` — never conflate these either

``None`` means "all accessible" (unbounded); ``[]`` means "match nothing" (empty, but
bounded and fully enumerable). The CLAUDE.md for this package is explicit that mapping
an unbounded scope is not possible at all — there is no enumerated list to check — so
:func:`check_scope_coverage` refuses to grade one (``applicable=False``) rather than
silently reporting 100% or 0%, either of which would be a claim about a check that never
ran. An empty scope, in contrast, is a real (degenerate) scope: zero files in, zero
files touched, complete by construction — but a hit appearing anyway is a **leak**, not
coverage, and is graded as incomplete via ``files_out_of_scope`` below.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

#: ``coverage`` dict keys that name a REASONED, counted absence — see the module
#: docstring. A key present in a caller's ``coverage`` dict but not in this tuple is
#: simply not recognised as an accounted reason: an unfamiliar/misspelled key cannot
#: silently manufacture coverage, which is the fail-closed direction this check exists
#: to enforce.
ACCOUNTED_KEYS: tuple[str, ...] = ("files_without_artifacts", "files_no_content")


@dataclass(frozen=True)
class ScopeCoverage:
    """The result of reconciling a resolved scope against one map's hits.

    Attributes:
        applicable: ``False`` for an unbounded scope (``file_uuids is None``) — there
            is nothing to check, and ``complete``/``scope_size`` carry no meaning.
        scope_size: Number of distinct files in the resolved scope. ``None`` only when
            ``applicable`` is ``False``.
        files_touched: Uuids the map produced at least one hit for.
        files_missing: In scope, but the map produced no hit for them.
        files_out_of_scope: The map produced a hit for a uuid NOT in the resolved
            scope — a leak, not a coverage gap; always empty in a correct caller.
        accounted_gap: ``sum(coverage[key] for key in ACCOUNTED_KEYS)`` — the number of
            scope members the caller's own ``coverage`` dict names a reason for.
        unaccounted: ``len(files_missing) - accounted_gap``. Zero means every missing
            file has a named reason; positive means some do not (the silent-gap shape
            this module exists to catch); negative means the ``coverage`` dict claims
            MORE accounted-for files than are actually missing, which is a bookkeeping
            bug in the caller, not something to hide.
        complete: ``None`` when not applicable. Otherwise ``True`` only when
            ``unaccounted == 0`` AND ``files_out_of_scope`` is empty — the file set the
            map drew on equals the resolved scope, or is a documented, reasoned subset.
        reason: Empty when complete (or not applicable); otherwise a human-readable
            summary naming the actual uuids involved, never just a count.
    """

    applicable: bool
    scope_size: int | None
    files_touched: frozenset[str] = field(default_factory=frozenset)
    files_missing: frozenset[str] = field(default_factory=frozenset)
    files_out_of_scope: frozenset[str] = field(default_factory=frozenset)
    accounted_gap: int = 0
    unaccounted: int = 0
    complete: bool | None = None
    reason: str = ""


def _describe_gap(missing: frozenset[str], out_of_scope: frozenset[str], accounted_gap: int) -> str:
    parts: list[str] = []
    unaccounted = len(missing) - accounted_gap
    if unaccounted > 0:
        parts.append(
            f"{unaccounted} of {len(missing)} missing file(s) have no counted reason: "
            f"{sorted(missing)}"
        )
    elif unaccounted < 0:
        parts.append(
            f"coverage dict claims {accounted_gap} accounted-for files but only "
            f"{len(missing)} are actually missing ({sorted(missing)}) — the accounting "
            "itself is wrong, not just incomplete"
        )
    if out_of_scope:
        parts.append(
            f"{len(out_of_scope)} file(s) produced a hit while OUT of the resolved "
            f"scope (a leak): {sorted(out_of_scope)}"
        )
    return "; ".join(parts)


def check_scope_coverage(
    file_uuids: list[str] | None,
    hits: Iterable[Any],
    coverage: dict[str, Any] | None = None,
) -> ScopeCoverage:
    """Does the map's output cover the resolved scope, or name every gap?

    Args:
        file_uuids: The resolved scope exactly as passed to
            :func:`~app.services.chat.mapreduce.scope_digest_hits` (or the speaker/
            document variants) — ``None`` for "all accessible" (unbounded, ungradeable),
            ``[]`` for "match nothing" (bounded, trivially complete).
        hits: The map's output. Only ``hit.file_uuid`` is read, so this accepts real
            ``ChunkHit`` instances, any duck-typed stand-in, or a plain iterable of
            ``(file_uuid, ...)`` — the caller decides.
        coverage: The map's own ``.coverage`` dict (``DigestScopeHits.coverage``), or
            ``None`` to treat every scope member with no hit as unaccounted (the
            strictest possible reading — nothing is presumed reasoned unless named).

    Returns:
        A :class:`ScopeCoverage`. Never raises — an unbounded scope is reported as
        ``applicable=False``, not an error, because refusing to answer a question that
        cannot be asked is the honest outcome, not a failure.
    """
    if file_uuids is None:
        return ScopeCoverage(
            applicable=False,
            scope_size=None,
            complete=None,
            reason=(
                "file_uuids is None ('all accessible', unbounded) — mapping over an "
                "unbounded scope is not possible, so there is no coverage claim to "
                "check here. See services/chat/mapreduce/CLAUDE.md: 'ranking is not "
                "mapping', and the ranked leg is the documented fallback for exactly "
                "this case."
            ),
        )

    scope = frozenset(str(uuid) for uuid in file_uuids)
    touched = frozenset(str(hit.file_uuid) for hit in hits)
    missing = scope - touched
    out_of_scope = touched - scope

    gap = coverage or {}
    accounted_gap = sum(int(gap.get(key, 0) or 0) for key in ACCOUNTED_KEYS)
    unaccounted = len(missing) - accounted_gap
    complete = unaccounted == 0 and not out_of_scope

    return ScopeCoverage(
        applicable=True,
        scope_size=len(scope),
        files_touched=touched,
        files_missing=missing,
        files_out_of_scope=out_of_scope,
        accounted_gap=accounted_gap,
        unaccounted=unaccounted,
        complete=complete,
        reason="" if complete else _describe_gap(missing, out_of_scope, accounted_gap),
    )


class ScopeCoverageError(AssertionError):
    """Raised by :func:`assert_full_coverage` — a real, named gap, not a style nit."""


def assert_full_coverage(
    file_uuids: list[str] | None,
    hits: Iterable[Any],
    coverage: dict[str, Any] | None = None,
) -> ScopeCoverage:
    """The hard-assertion form: raise unless the map's output is complete.

    Raises only when the check is applicable AND incomplete. An unbounded scope
    (``file_uuids is None``) returns its (non-applicable) result rather than raising —
    there being no map to check is not the defect this guards against; a bounded scope
    with an unexplained gap is.

    Returns:
        The :class:`ScopeCoverage`, so a caller that wants to inspect a non-applicable
        or already-complete result does not have to call :func:`check_scope_coverage`
        a second time.

    Raises:
        ScopeCoverageError: The scope was gradeable and incomplete. The message names
            the actual missing/leaked uuids, not just a count.
    """
    result = check_scope_coverage(file_uuids, hits, coverage)
    if result.applicable and not result.complete:
        raise ScopeCoverageError(
            f"scope coverage incomplete over {result.scope_size} file(s): {result.reason}"
        )
    return result
