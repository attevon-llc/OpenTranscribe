"""The SSE frame contract between `service.py` and `chatStream.ts`, enforced structurally.

Two frozen contracts, each with a documented failure mode if it silently drifts:

1. **Frame names.** `chatStream.ts`'s `createSseParser` drops any event whose
   name is not in its `known` allowlist (forward-compatibility with an older
   client) — so a backend-only change that emits a new frame name ships a
   frame nobody ever sees, with no error anywhere. The set of names
   `service.py` actually emits via `sse(...)` must be a SUBSET of that
   allowlist.
2. **Warning codes.** `schemas.chat.ChatWarningCode` (backend) and
   `lib/types/chat.ts`'s `ChatWarningCode` (frontend) must name the exact same
   codes — a code on only one side means either the server reports a problem
   nobody renders, or the client claims to handle a code the server never
   sends.

Both are checked by parsing the REAL source files, not by hand-copying the
expected sets here — a hand-copied set would drift exactly the way the two
originals did.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.schemas.chat import ChatWarningCode

pytestmark = pytest.mark.unit

_BACKEND_ROOT = Path(__file__).resolve().parents[2] / "app"
_FRONTEND_SRC = Path(__file__).resolve().parents[3] / "frontend" / "src"

_SERVICE_PY = _BACKEND_ROOT / "services" / "chat" / "service.py"
_CHAT_STREAM_TS = _FRONTEND_SRC / "lib" / "api" / "chatStream.ts"
_CHAT_TYPES_TS = _FRONTEND_SRC / "lib" / "types" / "chat.ts"

_STRING_LITERAL_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _string_literals(text: str) -> set[str]:
    return {a or b for a, b in _STRING_LITERAL_RE.findall(text)}


# ---------------------------------------------------------------------------
# Extraction — pure functions over SOURCE TEXT, so the must-fire controls
# below can feed them synthetic snippets instead of touching real files.
# ---------------------------------------------------------------------------


def backend_sse_event_names(source: str) -> set[str]:
    """Every literal event name passed as the first arg to `sse(...)`."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "sse"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def frontend_known_event_names(source: str) -> set[str]:
    """The `known` allowlist array in `chatStream.ts`'s SSE parser."""
    match = re.search(r"const\s+known\s*=\s*\[(.*?)\];", source, re.DOTALL)
    assert match is not None, "chatStream.ts's `known` array was not found — did it move?"
    return _string_literals(match.group(1))


def frontend_warning_codes(source: str) -> set[str]:
    """The `ChatWarningCode` union in `lib/types/chat.ts`."""
    match = re.search(r"export type ChatWarningCode\s*=([^;]*);", source, re.DOTALL)
    assert match is not None, "types/chat.ts's ChatWarningCode union was not found — did it move?"
    return _string_literals(match.group(1))


# ---------------------------------------------------------------------------
# Real contract checks
# ---------------------------------------------------------------------------


def test_every_backend_sse_frame_name_is_in_the_frontend_allowlist():
    backend_events = backend_sse_event_names(_SERVICE_PY.read_text(encoding="utf-8"))
    frontend_known = frontend_known_event_names(_CHAT_STREAM_TS.read_text(encoding="utf-8"))

    # If this is empty the AST scan broke, not "service.py emits no frames" —
    # a real chat turn emits at least start/status/delta/done.
    assert backend_events, (
        "scanner found zero sse(...) calls in service.py — check it still matches"
    )

    missing = backend_events - frontend_known
    assert not missing, (
        f"service.py emits frame(s) {sorted(missing)} that chatStream.ts's `known` "
        "list does not include — those frames are silently dropped on arrival. "
        "Add them to the `known` array in chatStream.ts."
    )


def test_backend_and_frontend_chat_warning_codes_match_exactly():
    backend_codes = {member.value for member in ChatWarningCode}
    frontend_codes = frontend_warning_codes(_CHAT_TYPES_TS.read_text(encoding="utf-8"))

    assert backend_codes, "ChatWarningCode has no members — check the import"
    assert backend_codes == frontend_codes, (
        f"backend-only: {sorted(backend_codes - frontend_codes)}; "
        f"frontend-only: {sorted(frontend_codes - backend_codes)}"
    )


# ---------------------------------------------------------------------------
# Must-fire controls: prove the detectors actually detect, on synthetic
# source that never touches the real files (backend/tests/CLAUDE.md's rule
# that an auditor needs a must-fire case, or a zero-finding result is
# indistinguishable from "nothing to find").
# ---------------------------------------------------------------------------


def test_the_backend_scanner_finds_a_synthetic_sse_call():
    source = """
def sse(event, payload):
    return event

def stream_reply():
    yield sse("start", {})
    yield sse("made_up_frame", {"x": 1})
"""
    assert backend_sse_event_names(source) == {"start", "made_up_frame"}


def test_the_backend_scanner_ignores_a_differently_named_call():
    """A call to something else named `sse` in an unrelated scope (e.g. an
    attribute access `foo.sse(...)`) must not be mistaken for the module
    helper — this is the same discipline `test_audit_event_emitters.py` uses
    for `AuditEventType.<member>` attribute access."""
    source = """
def stream_reply(foo):
    yield foo.sse("not_a_real_frame", {})
"""
    assert backend_sse_event_names(source) == set()


def test_the_frontend_known_list_parser_finds_synthetic_entries():
    source = """
    const known = [
      'start',
      'made_up_frame',
    ];
    """
    assert frontend_known_event_names(source) == {"start", "made_up_frame"}


def test_a_frame_missing_from_a_synthetic_known_list_is_detected_as_missing():
    backend_events = backend_sse_event_names(
        'def sse(e, p): pass\ndef f():\n    yield sse("start", {})\n    yield sse("rogue", {})\n'
    )
    frontend_known = frontend_known_event_names("const known = ['start'];")

    missing = backend_events - frontend_known
    assert missing == {"rogue"}


def test_the_warning_code_parser_finds_synthetic_union_members():
    source = "export type ChatWarningCode = 'a' | 'b' | 'c';\n"
    assert frontend_warning_codes(source) == {"a", "b", "c"}


def test_a_warning_code_present_on_only_one_side_is_detected():
    backend_codes = {"a", "b", "c"}
    frontend_codes = frontend_warning_codes("export type ChatWarningCode = 'a' | 'b';\n")
    assert backend_codes - frontend_codes == {"c"}


# ---------------------------------------------------------------------------
# ChatWarningCode: the specific members Wave 2 adds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "member,value",
    [
        (ChatWarningCode.AMBIGUOUS_SPEAKER, "ambiguous_speaker"),
        (ChatWarningCode.RECURRENCE_UNAVAILABLE, "recurrence_unavailable"),
        (ChatWarningCode.PLAN_FAILED, "plan_failed"),
        (ChatWarningCode.ROUTER_LANGUAGE_UNMATCHED, "router_language_unmatched"),
    ],
)
def test_the_new_warning_code_members_exist_with_the_expected_value(member, value):
    assert member.value == value
    assert member in ChatWarningCode


def test_the_pre_existing_warning_codes_are_unchanged():
    assert ChatWarningCode.CONTEXT_DROPPED.value == "context_dropped"
    assert ChatWarningCode.NO_CONTEXT.value == "no_context"
    assert ChatWarningCode.RETRIEVAL_FAILED.value == "retrieval_failed"
    assert ChatWarningCode.UNSUPPORTED_LANGUAGE.value == "unsupported_language"
