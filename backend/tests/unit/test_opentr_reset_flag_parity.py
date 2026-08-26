"""`reset_and_init()` must parse every `--with-*`/`--no-*` overlay flag `start_app()` does.

Found live: `./opentr.sh reset dev --with-mock-llm --with-llm-test` silently started
neither container. `start_app()` and `reset_and_init()` each hand-declare their own
`WITH_*_FLAG=""` variable block and their own `case "$1" in ...` parser — the exact
duplicated-block shape that already produced one crash in this file
(`WITH_MOCK_LLM_FLAG: unbound variable`, fixed in 690fb3d8, where `reset_and_init()`'s
variable-declaration block had silently drifted out of sync with `start_app()`'s). That
fix only added the missing variable declarations; it never checked whether the *case
branches* that populate them from CLI flags existed too. They didn't, for four flags:
`--with-mock-llm`, `--with-diar-native`, `--no-diar-native`, `--with-llm-test`.

The failure mode is worse than a crash: `reset_and_init()`'s parser has a catch-all
`*) echo "⚠️  Unknown flag: $1"` branch, so an unhandled flag prints a warning and is
silently dropped rather than aborting — easy to miss in a long build/start log, and the
container that was supposed to start (mock-llm, a real vLLM for `--with-llm-test`) simply
never does, with no test able to tell the difference between "flag worked" and "flag was
silently ignored" from the container's own healthy-or-not state, since it never starts
trying either way.

This test derives both parsers' flag sets from the actual `case` statement text (not a
hand-written list — a hand-written list would drift exactly the way the parsers did) and
asserts they match, modulo the flags `reset_and_init()` deliberately REJECTS outright
(`--fresh`/`--port-offset`/`--seed-benchmark` — reset deletes data, so resetting a
"fresh"/isolated deployment would reset the real stack instead, a documented footgun this
repo already guards against with an explicit early exit).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR_SH = REPO_ROOT / "opentr.sh"

#: Flags reset_and_init() explicitly refuses (see its own --fresh|--port-offset|
#: --seed-benchmark branch) rather than silently drops — a deliberate design
#: decision, not the drift bug this test guards against.
DELIBERATELY_REJECTED_BY_RESET = {"--fresh", "--port-offset", "--seed-benchmark"}

_FUNCTION_RE = re.compile(r"^(\w+)\(\)\s*\{", re.MULTILINE)
_CASE_FLAG_RE = re.compile(r"^\s*(--[a-z][a-z0-9-]*)(?:\|(--[a-z][a-z0-9-]*))*\)\s*$", re.MULTILINE)


def _function_body(source: str, name: str) -> str:
    """Extract one top-level `name() { ... }` function body via brace matching.

    A regex spanning to the next top-level `^}` (like the `awk` one-liners used to
    explore this bug live) is wrong in general — a nested `if ... fi` block's own
    closing brace-free syntax is fine, but any nested `{ ... }` (a subshell, a brace
    group) would end the match early. Brace-counting from the function's own `{` is
    the only way that is not fooled by the first coincidental closing pattern.
    """
    for match in _FUNCTION_RE.finditer(source):
        if match.group(1) != name:
            continue
        start = match.end() - 1  # position of the opening `{`
        depth = 0
        for index in range(start, len(source)):
            char = source[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return source[start : index + 1]
        raise AssertionError(f"unbalanced braces scanning {name}() in {OPENTR_SH}")
    raise AssertionError(f"{name}() not found in {OPENTR_SH}")


def _case_flags(function_body: str) -> set[str]:
    """Every `--flag)` / `--flag|--other)` case-branch pattern in a function body."""
    flags: set[str] = set()
    for match in _CASE_FLAG_RE.finditer(function_body):
        flags.update(group for group in match.groups() if group)
    return flags


def test_the_function_extractor_finds_the_real_functions() -> None:
    """Guard on the guard: a brace-matcher that finds nothing would pass every case below."""
    source = OPENTR_SH.read_text(encoding="utf-8")
    start_body = _function_body(source, "start_app")
    reset_body = _function_body(source, "reset_and_init")
    assert "--with-mock-llm" in start_body
    assert "ENVIRONMENT=${1:-dev}" in reset_body, "found the wrong function"


def test_the_case_flag_extractor_can_actually_fail() -> None:
    """Guard on the guard: a regex matching everything is not a check."""
    assert "--not-a-real-flag" not in _case_flags(
        "      --with-mock-llm)\n        shift\n        ;;\n"
    )


def test_reset_and_init_parses_every_overlay_flag_start_app_does() -> None:
    """The #690fb3d8-shaped assertion: derive both flag sets from the real parsers."""
    source = OPENTR_SH.read_text(encoding="utf-8")
    start_flags = _case_flags(_function_body(source, "start_app"))
    reset_flags = _case_flags(_function_body(source, "reset_and_init"))

    assert start_flags, "start_app()'s case statement matched no flags — the walk broke"
    assert reset_flags, "reset_and_init()'s case statement matched no flags — the walk broke"

    missing = start_flags - reset_flags - DELIBERATELY_REJECTED_BY_RESET
    assert not missing, (
        f"reset_and_init() does not parse these flags that start_app() does: "
        f"{sorted(missing)}. Each one either crashes under `set -u` (if referenced "
        f"without a declared default) or is silently dropped by the catch-all "
        f"'Unknown flag' branch (if declared but never given a case arm) — "
        f"'./opentr.sh reset dev --with-mock-llm --with-llm-test' did the latter, "
        f"starting neither container with no error."
    )
