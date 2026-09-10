"""``as_assert`` runs ``eval "$*"``, so its expression must never be pre-interpolated.

``test-lite-mode.sh`` asserted the chat reply was non-empty with::

    as_assert "chat summary non-empty" "[[ -n \\"$answer\\" ]]"

The expression is **double-quoted**, so ``$answer`` — the LLM's own output — is substituted
into the command text *before* ``eval`` parses it. The mock reply contains a markdown code
fence, and those backticks are command substitution, so the harness executed the model's
output (2026-09-07)::

    assertions.sh: line 66: python: command not found
    assertions.sh: command substitution: line 68: syntax error near unexpected token
        `'hello from the mock LLM''

Two defects at once: untrusted output is run as shell, and the assertion proved nothing —
the eval failed instead of testing emptiness, yet the scenario still reported 17/17.

⚠️ **The distinction is the QUOTE, not the presence of a variable**, and the other callers
rely on it. ``as_assert "R-6..." '[[ -z "$leaked" ]]'`` is SINGLE-quoted: ``eval`` receives
the literal text ``[[ -z "$leaked" ]]`` and expands ``$leaked`` *inside* ``[[ ]]``, where a
value is not re-parsed for command substitution. That form is safe and must keep working —
a rule of "no ``$`` in an as_assert argument" would fire on all of them and be deleted.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_TESTS = REPO_ROOT / "scripts" / "release-tests"
ASSERTIONS = RELEASE_TESTS / "lib" / "assertions.sh"

pytestmark = pytest.mark.skipif(
    not ASSERTIONS.is_file() or shutil.which("bash") is None,
    reason="scripts/release-tests/lib/assertions.sh or bash is not present in this checkout",
)

# as_assert "<label>" "<expr>"  where <expr> is DOUBLE-quoted and contains an expansion.
# Single-quoted expressions are the safe form and are deliberately not matched.
_UNSAFE = re.compile(r'as_assert\s+"[^"]*"\s+"[^\']*\$')


def _offenders() -> list[str]:
    out: list[str] = []
    for script in sorted(RELEASE_TESTS.rglob("*.sh")):
        for n, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if _UNSAFE.search(line):
                out.append(f"{script.relative_to(REPO_ROOT)}:{n}: {line.strip()[:120]}")
    return out


def test_no_as_assert_expression_is_pre_interpolated():
    offenders = _offenders()
    assert not offenders, (
        "these as_assert calls DOUBLE-quote the expression, so the variable is substituted "
        "into the command text before `eval` parses it — any backtick or $() in the value "
        "is then executed. Single-quote the expression (so the expansion happens inside "
        "[[ ]]), or use a non-eval assertion such as as_assert_ne:\n  " + "\n  ".join(offenders)
    )


def test_the_detector_distinguishes_the_two_quotings(tmp_path: Path):
    """Guard the guard, and pin the boundary the safe callers depend on."""
    unsafe = 'as_assert "chat summary non-empty" "[[ -n \\"$answer\\" ]]"'
    assert _UNSAFE.search(unsafe), (
        "the detector no longer matches the exact line that executed model output"
    )

    safe = 'as_assert "R-6: no post-FROM-migration table survives" \'[[ -z "$leaked" ]]\''
    assert not _UNSAFE.search(safe), (
        "the detector fires on the SINGLE-quoted form, which is safe and is what every "
        "other caller uses — a rule that flags those would be deleted rather than obeyed"
    )

    no_expansion = 'as_assert "static" "[[ 1 -eq 1 ]]"'
    assert not _UNSAFE.search(no_expansion), (
        "the detector fires on a double-quoted expression with no expansion, which cannot "
        "carry injected data"
    )


def _run(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_unsafe_form_really_executes_the_value(tmp_path: Path):
    """The must-fire control: prove the hazard rather than describing it.

    ⚠️ The side effect must be a FILE, not an echo. Command substitution captures stdout,
    so an injected `echo` is swallowed into the expression and leaves no trace — the real
    incident was only visible because `python` did not exist and complained to stderr. A
    control built on echo passes while proving nothing, which is the failure mode this
    whole module is about.
    """
    marker = tmp_path / "injected.txt"
    result = _run(f"""
        source "{ASSERTIONS}" 2>/dev/null || true
        as_record() {{ echo "$1|$2"; }}
        answer='pre `touch {marker}` post'
        as_assert "unsafe" "[[ -n \\"$answer\\" ]]"
    """)
    assert marker.exists(), (
        "the double-quoted form no longer executes text from the value — bash/eval "
        "semantics changed, and this module plus the fix it protects should be re-checked "
        f"rather than left guarding nothing.\n{result.stdout}{result.stderr}"
    )


def test_the_replacement_does_not_execute_the_value(tmp_path: Path):
    """as_assert_ne compares values; nothing is ever parsed as a command."""
    marker = tmp_path / "injected.txt"
    result = _run(f"""
        source "{ASSERTIONS}" 2>/dev/null || true
        as_record() {{ echo "$1|$2"; }}
        answer='pre `touch {marker}` post'
        as_assert_ne "chat summary non-empty" "" "$answer"
    """)
    combined = result.stdout + result.stderr
    assert not marker.exists(), f"as_assert_ne executed content from the value:\n{combined}"
    assert combined.startswith("PASS|"), (
        f"a non-empty answer must still PASS — the fix must keep the original meaning: {combined!r}"
    )


def test_the_replacement_still_fails_on_an_empty_answer():
    """The assertion has to be able to fail, which the broken eval version could not."""
    result = _run(f"""
        source "{ASSERTIONS}" 2>/dev/null || true
        as_record() {{ echo "$1|$2"; }}
        answer=''
        as_assert_ne "chat summary non-empty" "" "$answer"
    """)
    assert result.stdout.startswith("FAIL|"), (
        f"an empty chat answer no longer fails, so the assertion proves nothing: {result.stdout!r}"
    )
