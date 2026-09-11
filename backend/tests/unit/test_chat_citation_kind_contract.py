"""The citation-`kind` contract between `citations.py` and `types/chat.ts` (#830).

A `Citation.kind` value with frontend rendering support but no backend emitter is
dead weight at best and a silent feature gap at worst — issue #830 found exactly
that for `"recurrence"`: `ChatSources.svelte` rendered a whole badge/count/link
branch for it, three i18n keys carried it into all 12 locales, and there was a
dedicated vitest file exercising it — all against hand-built test payloads the
real pipeline never produces, because nothing under `app/services/chat/` ever
emitted one.

That is a DIFFERENT defect from the mirror-image failure this guard also has to
catch: a kind the BACKEND emits that the frontend cannot render at all, which
would be a silent styling/attribution gap rather than dead code.

**The line this guard enforces, and why it is not arbitrary:** a UI seam may
outlive its emitter only when an OPEN, MILESTONED issue names that emitter.
`"summary"` currently has no backend emitter either (see `KINDS_PENDING_AN_EMITTER`
below), but issue #464 (open, v0.6.0) schedules one — LLM summaries as the
map-reduce output — so it is a documented seam, not a gap. `"recurrence"` had no
such issue scheduling its emitter, which is why #830 deleted it rather than
building one.

Modelled on `test_chat_sse_contract.py`: parse the REAL source on both sides
rather than hand-copying the expected sets, because a hand-copied set drifts
exactly the way `ChatSourceKind` and `citations.py`'s `KIND_*` constants did.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.services.chat import citations

pytestmark = pytest.mark.unit

_FRONTEND_SRC = Path(__file__).resolve().parents[3] / "frontend" / "src"
_CHAT_TYPES_TS = _FRONTEND_SRC / "lib" / "types" / "chat.ts"
_CITATIONS_PY = Path(citations.__file__)

_STRING_LITERAL_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _string_literals(text: str) -> set[str]:
    return {a or b for a, b in _STRING_LITERAL_RE.findall(text)}


#: Every citation kind with a UI seam but NO backend emitter, and the reason it
#: is allowed to exist without one. Keyed by kind (not by test/file) because the
#: whole point is "this exact string is exempt", not "something nearby is fine".
#: A STALE entry (the backend has since grown that emitter) must fail this test
#: too — see assertion 4 below, the `KNOWN_DEAD_DARK_SELECTOR_FILES` idiom from
#: `theme-parity.test.ts`.
KINDS_PENDING_AN_EMITTER: dict[str, str] = {
    "summary": (
        "#464 (open, v0.6.0) -- LLM summaries as the map-reduce output; that "
        "lane adds the emitter. See schemas/chat.py's Citation docstring."
    ),
}


def backend_citation_kinds(module: object) -> set[str]:
    """Every `KIND_*` constant's VALUE on the citations module.

    Reading constants (not scanning call sites) is deliberate: it is what lets
    assertion 6 below separately guard that every actual emission SITE goes
    through one of these constants rather than a fresh string literal.
    """
    return {value for name, value in vars(module).items() if name.startswith("KIND_")}


def frontend_citation_kinds(source: str) -> set[str]:
    """The `ChatSourceKind` union in `lib/types/chat.ts`."""
    match = re.search(r"export type ChatSourceKind\s*=([^;]*);", source, re.DOTALL)
    assert match is not None, "types/chat.ts's ChatSourceKind union was not found — did it move?"
    return _string_literals(match.group(1))


def citations_py_has_no_literal_kind_value(source: str) -> list[str]:
    """AST scan: every dict literal's `"kind"` key must be bound to a NAME
    (a `KIND_*` constant, or a local variable derived from one), never a
    string constant written inline. Returns the offending literal values, if
    any -- empty means clean.

    This is what keeps assertion 3 (backend_kinds <= frontend_kinds) actually
    checking something: if a citation dict could set `"kind": "recurrence"`
    inline, `backend_citation_kinds` (which only reads `KIND_*` constants)
    would never see it, and the whole contract would silently stop covering
    real emission sites.
    """
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=False):
            if not (isinstance(key, ast.Constant) and key.value == "kind"):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                offenders.append(value.value)
    return offenders


# ---------------------------------------------------------------------------
# Real contract checks
# ---------------------------------------------------------------------------


def test_backend_emits_at_least_one_citation_kind():
    # If this is empty the KIND_* scan broke, not "citations.py defines none" —
    # chunk/digest citations are the pipeline's baseline output.
    assert backend_citation_kinds(citations)


def test_frontend_declares_at_least_one_citation_kind():
    # If this is empty the union-parser broke, not "the frontend renders no kinds".
    assert frontend_citation_kinds(_CHAT_TYPES_TS.read_text(encoding="utf-8"))


def test_every_backend_emitted_kind_is_something_the_frontend_can_render():
    backend_kinds = backend_citation_kinds(citations)
    frontend_kinds = frontend_citation_kinds(_CHAT_TYPES_TS.read_text(encoding="utf-8"))

    missing = backend_kinds - frontend_kinds
    assert not missing, (
        f"citations.py emits kind(s) {sorted(missing)} that ChatSourceKind does not "
        "list -- ChatSources.svelte's default branch will render these as an "
        "ordinary chunk, misattributing derived/summary text as something a "
        "speaker said. Add the kind to ChatSourceKind in types/chat.ts."
    )


def test_every_frontend_kind_with_no_backend_emitter_is_an_explicitly_allowed_exception():
    """Exact match, both directions -- a STALE allowlist entry fails this too."""
    backend_kinds = backend_citation_kinds(citations)
    frontend_kinds = frontend_citation_kinds(_CHAT_TYPES_TS.read_text(encoding="utf-8"))

    unemitted = frontend_kinds - backend_kinds
    allowed = set(KINDS_PENDING_AN_EMITTER)

    assert unemitted == allowed, (
        f"unemitted frontend kind(s) not covered by KINDS_PENDING_AN_EMITTER: "
        f"{sorted(unemitted - allowed)}; allowlist entries that are now STALE "
        f"(the backend has grown that emitter, or the frontend no longer "
        f"declares it -- delete the entry): {sorted(allowed - unemitted)}"
    )


def test_every_pending_emitter_reason_names_an_issue():
    """The `audit-tests.py` mandatory-reason convention, applied to this
    allowlist too: a reason that doesn't name an issue is not a reason."""
    for kind, reason in KINDS_PENDING_AN_EMITTER.items():
        assert isinstance(reason, str) and reason.strip(), f"{kind}: empty reason"
        assert "#" in reason, f"{kind}: reason does not name an issue: {reason!r}"


def test_citations_py_sets_every_kind_through_a_named_constant():
    offenders = citations_py_has_no_literal_kind_value(_CITATIONS_PY.read_text(encoding="utf-8"))
    assert not offenders, (
        f'citations.py sets "kind" to a literal string {offenders} instead of a '
        "KIND_* constant -- that emission site is invisible to this contract test. "
        "Add a KIND_* constant and reference it instead."
    )


# ---------------------------------------------------------------------------
# Must-fire / must-stay-clean controls on SYNTHETIC source, so a parser that
# matches nothing can't pass silently (the `test_chat_sse_contract.py` rule).
# ---------------------------------------------------------------------------


def test_the_frontend_known_list_parser_finds_synthetic_entries():
    source = "export type ChatSourceKind = 'chunk' | 'digest' | 'made_up_kind';\n"
    assert frontend_citation_kinds(source) == {"chunk", "digest", "made_up_kind"}


def test_a_kind_missing_from_a_synthetic_frontend_union_is_detected_as_missing():
    class _FakeModule:
        KIND_CHUNK = "chunk"
        KIND_NEW = "brand_new_kind"

    backend_kinds = backend_citation_kinds(_FakeModule)
    frontend_kinds = frontend_citation_kinds("export type ChatSourceKind = 'chunk' | 'digest';\n")
    assert backend_kinds - frontend_kinds == {"brand_new_kind"}


def test_the_ast_scanner_flags_a_synthetic_literal_kind_value():
    source = 'x = {"kind": "sneaky_literal", "id": 1}\n'
    assert citations_py_has_no_literal_kind_value(source) == ["sneaky_literal"]


def test_the_ast_scanner_stays_clean_on_a_synthetic_name_reference():
    source = 'KIND_CHUNK = "chunk"\nx = {"kind": KIND_CHUNK, "id": 1}\n'
    assert citations_py_has_no_literal_kind_value(source) == []
