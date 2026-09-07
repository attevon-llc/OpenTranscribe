"""The SSE frame contract between `service.py` and `chatStream.ts`, enforced structurally.

Three frozen contracts, each with a documented failure mode if it silently drifts:

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
3. **Error-frame `code` values (GH #611).** #611's hypothesis was that a
   backend code path stuffs arbitrary text (a chunk of model output) into an
   `event: error` frame's `code` field. That turned out to be false — the
   real defect was a release-harness parsing bug, not a backend one — but
   nothing structurally PREVENTED that hypothesis from becoming true later:
   every `sse("error", {...})` call site hand-writes its `code` value with no
   shared constant. This guard makes it an enforced invariant instead of a
   coincidence: every `code` the backend emits must be a short literal slug
   (never a computed expression, never prose), and must be a SUBSET of the
   frontend's closed `ChatErrorCode` union — subset, not exact parity, because
   `llm_unconfigured`/`quota_exceeded`/`rate_limited` are synthesised
   CLIENT-side from HTTP status (`chatStream.ts`'s `mapStatusToErrorCode`) and
   `cancelled` is a client-side message status, not a frame code; none of
   those four ever appears in `sse("error", ...)`'s literal set on the
   backend, so a two-way-parity test (like the warning-code test above) would
   fail on them permanently. Do not "fix" this into a parity test.

All three are checked by parsing the REAL source files, not by hand-copying the
expected sets here — a hand-copied set would drift exactly the way the two
originals did.

⚠️ **The error-code check (#3) originally "checked" only a hardcoded
`{service.py, messages.py}` pair, not the tree.** A follow-up adversarial audit
proved that was a bypass, not a scope decision: moving an `sse("error", ...)`
emission to any third file was completely invisible to it, and two real,
already-existing emitters — `api/endpoints/files/subtitles.py` and
`api/endpoints/files/__init__.py` — had silently never been covered. The
matcher also only recognised a bare-name call (`sse(...)`), so an
attribute-access call (`chat_service.sse(...)`) was skipped too. Both are now
fixed: `_backend_error_codes_by_file()` walks every `app/**/*.py` file, and
`_is_sse_call_target` matches both call shapes. `test_every_backend_sse_frame_
name_is_in_the_frontend_allowlist` (#1) deliberately does NOT get the same
tree-wide widening — its `foo.sse(...)` case is a *documented* exclusion
(`test_the_backend_scanner_ignores_a_differently_named_call`): an unrelated
object with a coincidentally-named `.sse()` method must not be mistaken for
the frame-emitting helper. The error-code guard accepts that same false-positive
risk deliberately, because an attribute-access `sse("error", ...)` masquerading
as unrelated is exactly the shape a refactor into a shared helper produces, and
missing a real error-code emitter is the worse failure mode of the two.
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
_MESSAGES_PY = _BACKEND_ROOT / "api" / "endpoints" / "chat" / "messages.py"
_CHAT_STREAM_TS = _FRONTEND_SRC / "lib" / "api" / "chatStream.ts"
_CHAT_TYPES_TS = _FRONTEND_SRC / "lib" / "types" / "chat.ts"

_ERROR_CODE_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_ERROR_CODE_LEN = 40

_STRING_LITERAL_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _string_literals(text: str) -> set[str]:
    return {a or b for a, b in _STRING_LITERAL_RE.findall(text)}


def _iter_backend_python_files() -> list[Path]:
    """Every backend source file under `app/` — the error-code guard's real detection
    surface (see the module docstring's ⚠️ note on why this replaced a 2-file allowlist).
    """
    return sorted(_BACKEND_ROOT.rglob("*.py"))


# ---------------------------------------------------------------------------
# Extraction — pure functions over SOURCE TEXT, so the must-fire controls
# below can feed them synthetic snippets instead of touching real files.
# ---------------------------------------------------------------------------


def backend_sse_event_names(source: str) -> set[str]:
    """Every literal event name passed as the first arg to `sse(...)`.

    Deliberately bare-name-only (`isinstance(node.func, ast.Name)`), unlike
    `backend_sse_error_codes`'s `_is_sse_call_target` above: an attribute-access
    call like `foo.sse(...)` must NOT be mistaken for the module helper here — see
    `test_the_backend_scanner_ignores_a_differently_named_call`. That test's own
    scope is still just `service.py` (frame names are consumed by chatStream.ts's
    parser specifically, unlike the error-code slug/subset invariant, which the
    module docstring's ⚠️ note explains now applies tree-wide).
    """
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


_NON_LITERAL_CODE = "<non-literal>"


def _is_sse_call_target(func: ast.expr) -> bool:
    """True for a call to something literally named `sse` — a bare name
    (`sse(...)`) or an attribute access (`chat_service.sse(...)`, `self.sse(...)`).

    The bare-name-only version of this check silently skipped the attribute
    form, which is exactly the shape a refactor into a shared helper module
    produces (`app/services/chat/error_frames.py` in the audit that found this).
    Unlike `backend_sse_event_names` below — which deliberately stays bare-name-only,
    see its module-docstring note — an attribute-access `sse("error", ...)` call
    being mistaken for an unrelated same-named method is an acceptable
    false-positive risk here: missing a real error-code emitter is worse.
    """
    if isinstance(func, ast.Name):
        return func.id == "sse"
    if isinstance(func, ast.Attribute):
        return func.attr == "sse"
    return False


def backend_sse_error_codes(source: str) -> set[str]:
    """Every `code` value from an `sse("error", {...})` call in `source`.

    A `code` value that is present but is NOT a string constant — a computed
    expression such as `str(exc)` — is represented as the literal token
    `"<non-literal>"` rather than being silently skipped. That token
    deliberately fails `_ERROR_CODE_SLUG_RE`: the whole point of this
    extractor is to turn "the code is not a fixed literal" into a detectable
    finding, which is exactly the shape GH #611 hypothesised (answer prose,
    or any other computed string, landing in a `code` field). A call whose
    second argument is not a dict literal at all, or that has no `code` key,
    is treated the same way.
    """
    tree = ast.parse(source)
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and _is_sse_call_target(node.func)
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "error"
        ):
            continue
        payload = node.args[1]
        if not isinstance(payload, ast.Dict):
            codes.add(_NON_LITERAL_CODE)
            continue
        found_code_key = False
        for key, value in zip(payload.keys, payload.values, strict=False):
            if isinstance(key, ast.Constant) and key.value == "code":
                found_code_key = True
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    codes.add(value.value)
                else:
                    codes.add(_NON_LITERAL_CODE)
        if not found_code_key:
            codes.add(_NON_LITERAL_CODE)
    return codes


def frontend_error_codes(source: str) -> set[str]:
    """The `ChatErrorCode` union in `lib/types/chat.ts`."""
    match = re.search(r"export type ChatErrorCode\s*=([^;]*);", source, re.DOTALL)
    assert match is not None, "types/chat.ts's ChatErrorCode union was not found — did it move?"
    return _string_literals(match.group(1))


def _backend_error_codes_by_file() -> dict[str, list[Path]]:
    """Every `sse("error", ...)` `code` found across the real `app/` tree, with the
    file(s) each one came from — so a finding names an offender, not just a token."""
    by_file: dict[str, list[Path]] = {}
    for path in _iter_backend_python_files():
        for code in backend_sse_error_codes(path.read_text(encoding="utf-8")):
            by_file.setdefault(code, []).append(path)
    return by_file


def _relative_offenders(paths: list[Path]) -> str:
    return ", ".join(str(p.relative_to(_BACKEND_ROOT.parent)) for p in paths)


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


def test_every_error_code_the_backend_emits_is_a_short_literal_slug():
    """GH #611's hypothesis, enforced structurally: an `event: error` frame's
    `code` must always be a fixed, short, lowercase slug — never a computed
    expression, and never prose (answer text has spaces, `**bold**`, `[1]`
    markers, all of which fail the pattern below). Scanned across the WHOLE
    `app/` tree (see the module docstring's ⚠️ note), not just chat's own
    modules — a hardcoded file pair is exactly the bypass a follow-up audit
    found and this replaced."""
    by_file = _backend_error_codes_by_file()
    codes = set(by_file)

    assert codes, (
        "scanner found zero sse('error', ...) calls anywhere under app/ — check it still matches"
    )

    for code in sorted(codes):
        offenders = _relative_offenders(by_file[code])
        assert code != _NON_LITERAL_CODE, (
            f"found an sse('error', ...) call in {offenders} whose 'code' value is not a "
            "fixed string literal (e.g. a computed expression, or a missing 'code' "
            "key) — this is exactly the class of defect GH #611 hypothesised: "
            "arbitrary computed text landing in an error frame's code field. "
            "Use a short literal slug instead."
        )
        assert _ERROR_CODE_SLUG_RE.match(code), (
            f"error code {code!r} (in {offenders}) is not a short lowercase slug matching "
            f"{_ERROR_CODE_SLUG_RE.pattern!r}"
        )
        assert len(code) <= _MAX_ERROR_CODE_LEN, (
            f"error code {code!r} (in {offenders}) is longer than {_MAX_ERROR_CODE_LEN} chars"
        )


def test_backend_error_codes_are_a_subset_of_the_frontend_union():
    """SUBSET, not exact parity — deliberately, unlike the warning-code test
    above. `llm_unconfigured` / `quota_exceeded` / `rate_limited` are
    synthesised CLIENT-side from HTTP status in `chatStream.ts`, and
    `cancelled` is a client-side message status, not a frame `code` the
    backend ever emits. Demanding two-way parity would fail on all four
    permanently — do not "fix" this into an `==` test. Scanned tree-wide, same
    reasoning as the slug test above."""
    by_file = _backend_error_codes_by_file()
    backend_codes = set(by_file)
    frontend_codes = frontend_error_codes(_CHAT_TYPES_TS.read_text(encoding="utf-8"))

    assert backend_codes, (
        "scanner found zero sse('error', ...) calls anywhere under app/ — check it still matches"
    )

    missing = backend_codes - frontend_codes
    offenders_by_code = {code: _relative_offenders(by_file[code]) for code in sorted(missing)}
    assert not missing, (
        f"backend emits error code(s) {offenders_by_code} that frontend's "
        "ChatErrorCode union (frontend/src/lib/types/chat.ts) does not "
        "declare — those errors have no matching UI copy on arrival."
    )


def test_the_backend_python_file_walk_reaches_beyond_the_original_two_files():
    """Regression guard for the actual bypass this closed: a hardcoded 2-file
    list left a THIRD file's `sse("error", ...)` call completely invisible,
    proven against two real, already-existing emitters that were never scanned
    before this fix (`files/subtitles.py`, `files/__init__.py`)."""
    paths = set(_iter_backend_python_files())
    assert _SERVICE_PY in paths
    assert _MESSAGES_PY in paths
    subtitles_py = _BACKEND_ROOT / "api" / "endpoints" / "files" / "subtitles.py"
    files_init_py = _BACKEND_ROOT / "api" / "endpoints" / "files" / "__init__.py"
    assert subtitles_py in paths, "the tree walk no longer reaches files/subtitles.py"
    assert files_init_py in paths, "the tree walk no longer reaches files/__init__.py"


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


def test_the_backend_error_code_scanner_finds_a_synthetic_literal_code():
    source = """
def sse(event, payload):
    return event

def stream_reply():
    yield sse("error", {"code": "provider_error", "message": "boom"})
"""
    assert backend_sse_error_codes(source) == {"provider_error"}


def test_the_backend_error_code_scanner_flags_a_synthetic_non_literal_code():
    """The must-fire case for #611's actual hypothesis: a computed `code`
    value (here, `str(exc)` — the shape of "answer text ended up in the code
    field") must be detected, not silently skipped."""
    source = """
def sse(event, payload):
    return event

def stream_reply(exc):
    yield sse("error", {"code": str(exc), "message": "boom"})
"""
    assert backend_sse_error_codes(source) == {_NON_LITERAL_CODE}


def test_the_backend_error_code_scanner_finds_an_attribute_access_sse_call():
    """Must-fire for the attribute-call gap: `chat_service.sse("error", ...)` is
    the exact shape a refactor into a shared helper module produces, and the
    bare-name-only matcher used to skip it silently."""
    source = """
def stream_reply(chat_service, exc):
    yield chat_service.sse("error", {"code": str(exc), "message": "boom"})
"""
    assert backend_sse_error_codes(source) == {_NON_LITERAL_CODE}


def test_the_backend_error_code_scanner_ignores_an_unrelated_attribute_call():
    """Must-stay-clean sibling: a call to some OTHER object's method that merely
    happens not to be named `sse` is not swept up by the widened matcher."""
    source = """
def stream_reply(foo, exc):
    yield foo.not_sse("error", {"code": str(exc)})
"""
    assert backend_sse_error_codes(source) == set()


def test_the_backend_error_code_scanner_ignores_non_error_sse_calls():
    source = """
def sse(event, payload):
    return event

def stream_reply():
    yield sse("delta", {"content": "hello"})
    yield sse("start", {})
"""
    assert backend_sse_error_codes(source) == set()


def test_the_frontend_error_code_union_parser_finds_synthetic_members():
    source = "export type ChatErrorCode = 'a' | 'b' | 'c';\n"
    assert frontend_error_codes(source) == {"a", "b", "c"}


def test_a_synthetic_error_code_slug_pattern_rejects_prose():
    assert _ERROR_CODE_SLUG_RE.match("provider_error")
    assert not _ERROR_CODE_SLUG_RE.match("Based on the transcript excerpts")
    assert not _ERROR_CODE_SLUG_RE.match("**bold** [1]")
    assert not _ERROR_CODE_SLUG_RE.match("")


def test_a_synthetic_error_code_missing_from_the_frontend_union_is_detected():
    backend_codes = backend_sse_error_codes(
        'def sse(e, p): pass\ndef f():\n    yield sse("error", {"code": "rogue_code"})\n'
    )
    frontend_codes = frontend_error_codes("export type ChatErrorCode = 'provider_error';\n")
    missing = backend_codes - frontend_codes
    assert missing == {"rogue_code"}


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
