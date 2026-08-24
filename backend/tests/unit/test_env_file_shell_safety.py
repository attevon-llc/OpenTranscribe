"""`.env` files must be safe to SHELL-SOURCE, not just parseable by compose.

``opentr.sh`` does ``set -a; . ./.env; set +a``, so **every line in .env is
executed by bash**. Docker Compose has its own parser that takes everything
after ``=`` literally, so a line can be perfectly valid to compose and actively
broken to bash. Nothing tested that half, and it shipped:

    OIDC_SCOPES=openid email profile

bash reads that as "set OIDC_SCOPES=openid for the duration of the command
``email profile``" — so it tried to run ``email``, printed
``./.env: line 1512: email: command not found`` on every single stack
operation, and **OIDC_SCOPES was never actually set**.

The quieter twin, which produces NO error at all:

    LDAP_USER_SEARCH_FILTER=(sAMAccountName={username})

``VAR=(...)`` is bash ARRAY-assignment syntax. It succeeds, and the variable
silently becomes a one-element array instead of the intended string.

``test_env_example_coverage.py`` is the sibling gate and answers a different
question — whether documented keys are actually read. Neither of the two bugs
above would have failed it: both keys are documented and both are read.

⚠️ Inline comments are NOT a defect. bash treats ``#`` as starting a comment
when it follows whitespace, so ``PORT=5176  # the dev port`` is fine. A first
draft of this check flagged 41 lines, of which 39 were exactly that. It is
also why :func:`_value_of` strips the comment before judging, and why
``test_an_inline_comment_is_not_a_finding`` exists.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_LIVE = REPO_ROOT / ".env"

_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_COMMENTED_ASSIGN = re.compile(r"^\s*#\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
#: Characters bash acts on rather than storing. ``(`` / ``)`` are the array
#: case above; the rest split or redirect the command.
_SHELL_META = re.compile(r"[;&|<>()]")


def _value_of(raw_value: str) -> str:
    """The value bash would assign, with any trailing comment removed."""
    comment = re.search(r"(?:^|[ \t])#", raw_value)
    return (raw_value[: comment.start()] if comment else raw_value).strip()


def _is_quoted(value: str) -> bool:
    return len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]


def _unsafe_assignments(text: str, *, include_commented: bool) -> list[tuple[int, str, str]]:
    """Lines whose value bash would execute or mangle instead of storing."""
    findings: list[tuple[int, str, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped:
            continue
        match = _ASSIGN.match(raw)
        if match is None:
            if not include_commented:
                continue
            match = _COMMENTED_ASSIGN.match(raw)
            if match is None:
                continue
        key, value = match.group(1), _value_of(match.group(2))
        if not value or _is_quoted(value):
            continue
        reasons = []
        if re.search(r"\s", value):
            reasons.append("unquoted spaces (bash runs the rest as a command)")
        if _SHELL_META.search(value):
            reasons.append("unquoted shell metacharacter")
        if "`" in value or "$(" in value:
            reasons.append("command substitution")
        if reasons:
            findings.append((lineno, key, "; ".join(reasons)))
    return findings


def _duplicate_keys(text: str) -> dict[str, list[int]]:
    seen: dict[str, list[int]] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGN.match(raw)
        if match:
            seen.setdefault(match.group(1), []).append(lineno)
    return {k: v for k, v in seen.items() if len(v) > 1}


# ─── guard the guard ────────────────────────────────────────────────────────
# A checker that silently matches nothing is indistinguishable from a clean
# file, so each detector needs a must-fire case and a must-stay-clean case.


def test_the_real_oidc_scopes_bug_is_detected():
    """The exact line that shipped must be a finding."""
    findings = _unsafe_assignments("OIDC_SCOPES=openid email profile\n", include_commented=False)
    assert len(findings) == 1, f"the shipped bug was not detected: {findings}"
    assert findings[0][1] == "OIDC_SCOPES"
    assert "spaces" in findings[0][2]


def test_the_silent_array_assignment_bug_is_detected():
    """``VAR=(...)`` raises no error and must still be caught."""
    findings = _unsafe_assignments(
        "LDAP_USER_SEARCH_FILTER=(sAMAccountName={username})\n", include_commented=False
    )
    assert len(findings) == 1, f"array-assignment shape not detected: {findings}"
    assert "metacharacter" in findings[0][2]


def test_an_inline_comment_is_not_a_finding():
    """bash honours ``# comment`` after a value — flagging it is a false positive.

    This case exists because a first draft reported 41 findings of which 39
    were inline comments.
    """
    clean = "POSTGRES_PORT=5176  # the dev stack port\nMINIO_PORT=5178\t# storage\n"
    assert _unsafe_assignments(clean, include_commented=False) == []


def test_quoted_values_are_not_findings():
    """Quoting is the fix, so a quoted value must read as clean."""
    clean = "A=\"openid email profile\"\nB='(uid={username})'\nC=plainvalue\n"
    assert _unsafe_assignments(clean, include_commented=False) == []


def test_duplicate_detector_fires_and_ignores_comments():
    text = "PORT=1\n# PORT=2\nPORT=3\nOTHER=4\n"
    dups = _duplicate_keys(text)
    assert dups == {"PORT": [1, 3]}, f"duplicate detection wrong: {dups}"


# ─── the actual gates ───────────────────────────────────────────────────────


def test_env_example_is_safe_to_shell_source():
    """`.env.example` is what a new developer copies to `.env` — it must be clean.

    A broken line here becomes a broken line in every fresh deployment.
    """
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} is missing"
    findings = _unsafe_assignments(ENV_EXAMPLE.read_text(), include_commented=False)
    assert not findings, "\n".join(
        [".env.example has values bash would execute instead of store:"]
        + [
            f"  line {n}: {k} — {why}   (fix: wrap the value in double quotes)"
            for n, k, why in findings
        ]
    )


# ⚠️ DELIBERATELY NOT TESTED: whether COMMENTED-OUT examples are safe to
# uncomment.
#
# It sounds like the natural third gate and it cannot be made sound. A commented
# assignment is indistinguishable, by parsing, from PROSE that happens to contain
# ``KEY=value`` — and this file is full of the latter:
#
#     # POSTGRES_HOST=postgres (already set above)
#     # GPU_WORKER_POOL=prefork and accept that the model reloads on every task.
#
# Both are English sentences. A checker that demands they be quoted is demanding
# that documentation be corrupted, and an earlier draft of this module did
# exactly that to five lines before it was caught and reverted. The key name is
# no discriminator either: ``GPU_WORKER_POOL`` is a real setting in both shapes.
#
# So the commented examples are kept correct by REVIEW, not by a gate. If you
# reintroduce this check, it needs an allowlist with written reasons (the
# ``audit-tests.py`` pattern) — not a cleverer regex.


def test_env_example_has_no_duplicate_keys():
    """A duplicate is silent: the LAST definition wins, so editing the first does nothing."""
    dups = _duplicate_keys(ENV_EXAMPLE.read_text())
    assert not dups, "\n".join(
        [".env.example defines keys more than once (last one wins):"]
        + [f"  {k}: lines {v}" for k, v in sorted(dups.items())]
    )


def test_env_example_actually_sources_in_bash():
    """The end-to-end check: hand the file to bash and require silence.

    The static scan above encodes what we *believe* bash does; this asserts it.
    Run in a subshell with the values discarded, so nothing is printed.
    """
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set +u; set -a; . "$1" >/dev/null 2>/tmp/.env-src-err; set +a; cat /tmp/.env-src-err',
            "_",
            str(ENV_EXAMPLE),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    noise = result.stdout.strip()
    assert not noise, f".env.example produces errors when sourced by bash:\n{noise}"


@pytest.mark.skipif(not ENV_LIVE.is_file(), reason="no local .env (CI, or a fresh checkout)")
def test_the_local_env_is_safe_to_shell_source():
    """Developer-local guard. Skipped in CI, where `.env` does not exist.

    Not redundant with the `.env.example` gate: `.env` drifts independently —
    it is gitignored, hand-edited, and is where the original bug actually lived.
    """
    findings = _unsafe_assignments(ENV_LIVE.read_text(), include_commented=False)
    assert not findings, "\n".join(
        ["your local .env has values bash would execute instead of store:"]
        + [
            f"  line {n}: {k} — {why}   (fix: wrap the value in double quotes)"
            for n, k, why in findings
        ]
    )
