"""Every unguarded ``$VAR`` in ``opentr.sh`` + ``scripts/common.sh`` must be defaulted.

Both scripts run under ``set -uo pipefail``, where referencing an unset variable **aborts the
script**. That is the right setting — a silently empty ``--gpus device=`` is worse than a hard
stop — but it makes every optional ``.env`` variable a landmine:

``scripts/common.sh`` read a bare ``[ -n "$GPU_DEVICE_ID" ]`` while ``opentr.sh`` defaulted five
*other* optional variables and not that one. Result: ``./opentr.sh`` died with ``GPU_DEVICE_ID:
unbound variable`` in **any checkout without a ``.env``** — which is every git worktree, since
``.env`` is gitignored and never comes along, and every fresh clone whose ``.env`` omits the key.
So the failure blocked exactly the isolated-worktree testing workflow it was needed for
(issue #431, found while doing #403 in a worktree).

This is a **static** test: it parses the scripts and asserts the shape. It does not execute them,
so it costs milliseconds and runs in the fast unit suite, before anything tries to start a stack.
It is deliberately narrow — it proves every expansion is *guarded*, not that the defaults are
*correct*.

**Guard the guard.** A scanner that silently matches nothing passes everything, which is the
failure mode this repo has already shipped twice (a marker that selected no tests; an e2e
hygiene check comparing selectors by string equality and therefore finding none). So the
must-fire and must-stay-clean cases below are not ceremony: each encodes a shape that broke a
draft of this scanner — an escaped ``\\$VAR`` in help text, a ``$VAR`` inside single quotes, and
a ``$VAR`` in a comment were all reported as offenders before they were handled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The two scripts in scope. `common.sh` is sourced only by `opentr.sh`, and both run under
#: `set -u`, so they share one blast radius.
_SCRIPTS = ("opentr.sh", "scripts/common.sh")

#: Variables **bash itself** maintains, which therefore cannot be unset at reference time.
#: Deliberately excludes `USER`, `LOGNAME`, `TERM`, `LANG`, `TMPDIR` and `EDITOR`: those are
#: environment conveniences, not guarantees. `USER` is unset under `env -i`, in a bare container,
#: and under some cron and systemd units — and it is one of the real offenders below, so
#: excusing it here would have hidden the finding.
_BASH_MAINTAINED = frozenset(
    {
        "BASH",
        "BASHOPTS",
        "BASHPID",
        "BASH_REMATCH",
        "BASH_SOURCE",
        "BASH_SUBSHELL",
        "BASH_VERSINFO",
        "BASH_VERSION",
        "DIRSTACK",
        "EUID",
        "FUNCNAME",
        "GROUPS",
        "HOME",
        "HOSTNAME",
        "HOSTTYPE",
        "IFS",
        "LINENO",
        "MACHTYPE",
        "OSTYPE",
        "PATH",
        "PIPESTATUS",
        "PPID",
        "PWD",
        "RANDOM",
        "REPLY",
        "SECONDS",
        "SHELLOPTS",
        "SHLVL",
        "UID",
        "_",
    }
)

#: ``${VAR}``/``${VAR<op>…}`` and bare ``$VAR``. Positional and special parameters (``$1``,
#: ``$?``, ``$@``) are out of scope by construction — the name must start with a letter or
#: underscore. They are a different class: a missing argument wants a usage message, not a
#: default, and mixing the two would blur what this test proves.
#:
#: The optional ``[#!]`` prefix matches ``${#VAR}`` (length) and ``${!VAR}`` (indirect). Both
#: still abort under ``set -u``, and without the prefix the regex did not see them AT ALL — so
#: they were silently unscannable rather than reported. Its own must-fire case caught that.
_REF_RE = re.compile(r"\$\{[#!]?([A-Za-z_]\w*)([^}]*)\}|\$([A-Za-z_]\w*)")

#: The parameter expansions that supply a value, an alternative, or a deliberate error when the
#: variable is unset: ``:-`` ``-`` ``:=`` ``=`` ``:?`` ``?`` ``:+`` ``+``. Any of these is a
#: guard. ``${#VAR}`` and ``${VAR%…}`` are NOT — both still abort under ``set -u``.
_GUARD_OP_RE = re.compile(r"^:?[-=?+]")

_ASSIGN_RES = (
    # NAME=… / NAME+=… , optionally via export/readonly/declare/typeset/local
    re.compile(
        r"^\s*(?:export\s+|readonly\s+|declare\s+(?:-\w+\s+)*|typeset\s+(?:-\w+\s+)*"
        r"|local\s+(?:-\w+\s+)*)?([A-Za-z_]\w*)\+?="
    ),
    # bare declaration: `local name`, `export NAME`
    re.compile(r"^\s*(?:export|readonly|local|declare|typeset)\s+([A-Za-z_]\w*)\s*$"),
    re.compile(r"^\s*for\s+([A-Za-z_]\w*)\s+in\b"),
    re.compile(r"\bread\b[^;|&]*?\s([A-Za-z_]\w*)\s*$"),
    re.compile(r"\bmapfile\b.*?\s([A-Za-z_]\w*)\s*$"),
)

#: `: "${NAME:=}"` — the prologue idiom that gives an optional `.env` variable a value for the
#: whole run. `opentr.sh` has a block of these, and because it runs at TOP LEVEL before any
#: function is called, it also covers references inside `common.sh`.
_DEFAULT_BLOCK_RE = re.compile(r':\s*"?\$\{([A-Za-z_]\w*):?=')

#: `local a b c` — several names in one statement.
_LOCAL_RE = re.compile(r"^\s*local\s+(.*)$")


@dataclass(frozen=True)
class _Ref:
    """One unguarded variable reference."""

    script: str
    line: int
    name: str
    source: str

    @property
    def key(self) -> str:
        """Allowlist key. Keyed by variable, not line: line numbers move, the defect does not."""
        return f"{self.script}::{self.name}"


# ---------------------------------------------------------------------------------------------
# Allowlist. `<script>::<VAR>  ->  written reason`, and the reason is MANDATORY.
#
# A stale entry FAILS the test (see `test_no_stale_allowlist_entries`), so this dict can only
# shrink: default the variable, delete the line. An exemption cannot outlive its subject.
# ---------------------------------------------------------------------------------------------
#: Empty on purpose. All three findings this suite was written for are FIXED, not waived:
#: ``GPU_DEVICE_ID`` and ``ENVIRONMENT`` are now defaulted in ``opentr.sh``'s prologue block
#: and ``USER`` uses ``${USER:-$(id -un)}`` at the use site. The dict stays because a
#: genuinely un-defaultable expansion may appear later — but an entry must state the reason,
#: and a stale one fails :func:`test_the_allowlist_has_no_stale_entries`, so the list can
#: only ever shrink without someone noticing.
_ALLOWLIST: dict[str, str] = {}


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment, respecting quotes.

    ``echo "http://host/#anchor"`` must keep its ``#``; ``foo # $BAR`` must lose it, or a
    variable merely *named in prose* is reported as an expansion.
    """
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for char in line:
        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\":
            out.append(char)
            escaped = True
            continue
        if quote is not None:
            out.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#" and (not out or out[-1].isspace()):
            break
        out.append(char)
    return "".join(out)


def _expanding_spans(code: str) -> list[tuple[int, int]]:
    """Index ranges of ``code`` where a ``$`` actually expands.

    Two suppressors, both of which produced false positives in a draft:

    * **single quotes** — ``'$USER'`` is four literal characters;
    * **a backslash** — ``echo "run: sudo usermod -aG docker \\$USER"`` prints the variable's
      *name* as instructions to the operator. `opentr.sh` does this in its help text, and it
      was reported as an unguarded expansion.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    quote: str | None = None
    escaped = False
    for i, char in enumerate(code):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            # The escape suppresses the next character, so close the span before it.
            if quote != "'":
                spans.append((start, i))
                start = i + 2
            escaped = True
            continue
        if quote is None and char in "\"'":
            quote = char
            if char == "'":
                spans.append((start, i))
            continue
        if quote is not None and char == quote:
            if char == "'":
                start = i + 1
            quote = None
    spans.append((start, len(code)))
    return [(a, b) for a, b in spans if a < b]


def _assigned_names(text: str) -> set[str]:
    """Every name the script binds anywhere — assignment, loop variable, ``read``, ``local``."""
    names: set[str] = set()
    for line in text.splitlines():
        code = _strip_comment(line)
        for pattern in _ASSIGN_RES:
            names.update(match.group(1) for match in pattern.finditer(code))
        local = _LOCAL_RE.match(code)
        if local:
            for token in local.group(1).split():
                names.add(token.split("=")[0].lstrip("-"))
    return {name for name in names if re.fullmatch(r"[A-Za-z_]\w*", name)}


def _prologue_defaults(text: str) -> set[str]:
    """Names given a value by a top-level ``: "${NAME:=…}"``."""
    return {
        match.group(1)
        for line in text.splitlines()
        for match in _DEFAULT_BLOCK_RE.finditer(_strip_comment(line))
    }


def _unguarded_refs(script: str, text: str, safe: set[str]) -> list[_Ref]:
    """Unguarded references in one script, given the names already known to be bound."""
    found: list[_Ref] = []
    for number, line in enumerate(text.splitlines(), start=1):
        code = _strip_comment(line)
        spans = _expanding_spans(code)
        for match in _REF_RE.finditer(code):
            if not any(a <= match.start() < b for a, b in spans):
                continue
            braced, operator, bare = match.group(1), match.group(2), match.group(3)
            name = braced or bare
            if braced is not None and _GUARD_OP_RE.match(operator or ""):
                continue
            if name in _BASH_MAINTAINED or name in safe:
                continue
            found.append(_Ref(script, number, name, line.strip()[:110]))
    return found


def _scan_repo() -> list[_Ref]:
    """Unguarded references across both scripts.

    A reference is guarded by an assignment **in its own file**, or by a top-level
    ``: "${NAME:=…}"`` in *either* file — the prologue runs before any function is called, so it
    reaches `common.sh` too. An assignment inside a *function* of the other file does NOT count:
    that is precisely how `$ENVIRONMENT` slipped through, being set only inside `opentr.sh`'s
    `start` path while `common.sh` reads it unconditionally.
    """
    texts = {script: (_REPO_ROOT / script).read_text(encoding="utf-8") for script in _SCRIPTS}
    prologue = {name for text in texts.values() for name in _prologue_defaults(text)}
    found: list[_Ref] = []
    for script, text in texts.items():
        found.extend(_unguarded_refs(script, text, _assigned_names(text) | prologue))
    return found


# ---------------------------------------------------------------------------------------------
# The assertion this file exists for
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_every_expansion_is_guarded_or_allowlisted() -> None:
    """No new unguarded ``$VAR`` may land in either script.

    Both run under ``set -u``, so an unguarded optional `.env` variable is not a style nit — it
    is a hard abort in any checkout that does not define it.
    """
    offenders = [ref for ref in _scan_repo() if ref.key not in _ALLOWLIST]
    detail = "\n".join(f"  {r.script}:{r.line}  ${r.name}  -->  {r.source}" for r in offenders)
    assert not offenders, (
        f"{len(offenders)} unguarded expansion(s) under `set -u` — each aborts the script when "
        f"the variable is absent from .env:\n{detail}\n"
        'Fix: `${VAR:-default}` at the use site, or `: "${VAR:=}"` in the opentr.sh prologue.'
    )


@pytest.mark.unit
def test_no_stale_allowlist_entries() -> None:
    """An exemption whose offender is gone is an exemption nobody will ever delete."""
    live = {ref.key for ref in _scan_repo()}
    stale = sorted(set(_ALLOWLIST) - live)
    assert not stale, (
        f"{len(stale)} allowlist entry(ies) no longer match any unguarded expansion — the "
        f"variable was defaulted, so delete the entry: {stale}"
    )


@pytest.mark.unit
def test_allowlist_entries_carry_a_written_reason() -> None:
    """A reason is what makes an exemption reviewable rather than a parking space."""
    thin = sorted(key for key, reason in _ALLOWLIST.items() if len(reason.strip()) < 40)
    assert not thin, f"allowlist entries with no substantive reason: {thin}"


# ---------------------------------------------------------------------------------------------
# Guard the guard. Each case below is a shape that broke a draft of the scanner.
# ---------------------------------------------------------------------------------------------

_MUST_FIRE = (
    ("bare reference", 'echo "$GPU_DEVICE_ID"\n', "GPU_DEVICE_ID"),
    ("braced, no operator", 'echo "${GPU_DEVICE_ID}"\n', "GPU_DEVICE_ID"),
    ("test -n, the real bug", 'if [ -n "$GPU_DEVICE_ID" ]; then :; fi\n', "GPU_DEVICE_ID"),
    ("length is not a guard", 'echo "${#NGINX_SERVER_NAME}"\n', "NGINX_SERVER_NAME"),
    ("suffix strip is not a guard", 'echo "${MINIO_NAS_PATH%/}"\n', "MINIO_NAS_PATH"),
    ("inside a double-quoted string", 'echo "user is $USER today"\n', "USER"),
    ("single quotes nested in double", "echo \"got '$BENCH_TARGET'\"\n", "BENCH_TARGET"),
)

_MUST_STAY_CLEAN = (
    ("default operator", 'echo "${PROMETHEUS_PORT:-5186}"\n'),
    ("dash without colon", 'echo "${PROMETHEUS_PORT-5186}"\n'),
    ("assign-default", ': "${COMPOSE_PROFILES:=}"\n'),
    ("error operator is a deliberate abort", 'echo "${REQUIRED:?must be set}"\n'),
    ("alternate value", 'echo "${MAYBE:+--flag}"\n'),
    ("plus without colon", 'echo "${MAYBE+--flag}"\n'),
    ("escaped — printed as instructions, not expanded", 'echo "run: usermod -aG docker \\$USER"\n'),
    ("single-quoted is literal", "echo 'literal $USER stays literal'\n"),
    ("named in a comment only", "# remember to set $GPU_DEVICE_ID in .env\n"),
    ("assigned earlier in the same file", 'THING=1\necho "$THING"\n'),
    ("assigned via export", 'export BUILD_ENV="dev"\necho "$BUILD_ENV"\n'),
    ("a local", 'f() {\n  local use_gpu="false"\n  echo "$use_gpu"\n}\n'),
    ("a loop variable", 'for f in a b; do echo "$f"; done\n'),
    ("read binds the name", 'read -r answer\necho "$answer"\n'),
    ("bash maintains it", 'echo "$BASH_SOURCE $PWD $UID $RANDOM"\n'),
    ("positional params are out of scope", 'echo "$1 $2 $# $? $@"\n'),
    ("defaulted in the prologue block", ': "${NGINX_SERVER_NAME:=}"\necho "$NGINX_SERVER_NAME"\n'),
)


@pytest.mark.unit
@pytest.mark.parametrize(("label", "script", "name"), _MUST_FIRE, ids=[c[0] for c in _MUST_FIRE])
def test_scanner_reports_an_unguarded_expansion(label: str, script: str, name: str) -> None:
    """A scanner that matches nothing reports a clean tree. These prove it still matches."""
    found = _unguarded_refs("fixture.sh", script, _assigned_names(script))
    assert [ref.name for ref in found] == [name], (
        f"{label}: expected ${name} to be reported, got {[r.name for r in found] or 'nothing'}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "script"), _MUST_STAY_CLEAN, ids=[c[0] for c in _MUST_STAY_CLEAN]
)
def test_scanner_stays_silent_on_guarded_shapes(label: str, script: str) -> None:
    """The false-positive half. Three of these were real false positives on the real scripts."""
    safe = _assigned_names(script) | _prologue_defaults(script)
    found = _unguarded_refs("fixture.sh", script, safe)
    assert not found, f"{label}: falsely reported {[(r.name, r.source) for r in found]}"


@pytest.mark.unit
def test_scanner_actually_parses_the_real_scripts() -> None:
    """The corpus check: prove the scanner READ the scripts, not just failed to find anything.

    Asserted on *total* references rather than offenders on purpose — once the three known
    offenders are defaulted, an offender-based check would start failing for the right reason at
    the wrong time. A path typo or a rename would drop this to zero.
    """
    counts = {}
    for script in _SCRIPTS:
        text = (_REPO_ROOT / script).read_text(encoding="utf-8")
        refs = [m for line in text.splitlines() for m in _REF_RE.finditer(_strip_comment(line))]
        counts[script] = len(refs)
    thin = sorted(script for script, count in counts.items() if count < 20)
    assert not thin, f"scanner found almost no variable references in {thin} — counts: {counts}"
