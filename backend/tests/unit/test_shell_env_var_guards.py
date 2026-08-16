"""`./opentr.sh` must survive a checkout with no `.env`.

`opentr.sh` runs under ``set -uo pipefail`` and sources ``scripts/common.sh``
into that same shell, so an expansion of a variable neither file assigns is a
hard abort — not a warning, not an empty string. The script defends against this
with a defaults block near the top (``: "${FOO:=}"``), but that block is
hand-maintained and the consumers are 3,000 lines away in a different file, so
the two drift silently. Nothing notices, because the drift is invisible to
anyone whose `.env` happens to define the variable.

That is exactly what happened to ``GPU_DEVICE_ID``: ``common.sh`` tested it bare
(``[ -n "$GPU_DEVICE_ID" ]``) and the defaults block did not list it, so
``./opentr.sh start dev`` died with ``scripts/common.sh: line 231:
GPU_DEVICE_ID: unbound variable`` on any checkout without a `.env` — a fresh
clone, and *every git worktree*, since `.env` is gitignored and never comes
along. On a developer machine with a `.env` it was undetectable.

Two tests, one static and one real:

1. :func:`test_no_unguarded_expansion_is_missing_from_the_defaults_block` scans
   both files for expansions that are neither assigned locally nor written with
   a ``:-``/``:=``-style default, and requires each to be listed in the defaults
   block. This is the contract, and it is what fails when someone adds a new
   `.env` variable and forgets the block.
2. :func:`test_ensure_opensearch_models_survives_an_absent_env` actually runs the
   real ``ensure_opensearch_models`` under ``set -u`` with no `.env` and stubbed
   ``docker``/``nvidia-smi``, and asserts the shell does not abort. The static
   test encodes the rule; this one reproduces the bug.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"
COMMON = REPO_ROOT / "scripts" / "common.sh"

pytestmark = pytest.mark.skipif(
    not OPENTR.exists() or not COMMON.exists(),
    reason="opentr.sh / scripts/common.sh not present in this checkout",
)

# Names the shell itself always provides, so `set -u` can never trip on them.
# Deliberately short: anything an operator could reasonably fail to export does
# NOT belong here. `USER` in particular is absent on purpose — it is unset under
# some cron/container invocations, which is why common.sh falls back to `id -un`.
SHELL_PROVIDED = frozenset(
    {
        "BASH_SOURCE",
        "BASH_VERSION",
        "EUID",
        "FUNCNAME",
        "HOME",
        "HOSTNAME",
        "IFS",
        "LINENO",
        "OSTYPE",
        "PATH",
        "PIPESTATUS",
        "PWD",
        "RANDOM",
        "REPLY",
        "SECONDS",
        "SHELL",
        "TMPDIR",
        "UID",
    }
)

# A name is "assigned" if any of these match. Coarse on purpose: a false
# *negative* here only makes the test stricter than it needs to be, and the fix
# for a strict flag (add a `:-`) is correct regardless.
_ASSIGNMENT_PATTERNS = (
    # FOO=..., export FOO=..., local FOO=..., declare -a FOO=..., FOO[0]=..., FOO+=...
    re.compile(
        r"^\s*(?:export\s+|local\s+|declare\s+(?:-\w+\s+)*|readonly\s+|typeset\s+)?"
        r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?\+?="
    ),
    # bare `local FOO` / `export FOO` / `unset FOO`
    re.compile(r"^\s*(?:export|local|declare|readonly|unset)\s+([A-Za-z_][A-Za-z0-9_]*)\s*$"),
    re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b"),
    re.compile(r"\bread\s+(?:-\S+\s+)*([A-Za-z_][A-Za-z0-9_]*)"),
    # the defaults-block form itself: : "${FOO:=}"
    re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?="),
    # one-shot env prefix: FOO=bar some-command
    re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=\S*\s+\S"),
)

# ${FOO}, ${#FOO}, ${!FOO}, ${FOO[@]}, ${FOO:-x} ... and bare $FOO.
# The (?<!\\) rejects an escaped \$FOO, which is literal text in a double-quoted
# echo and not an expansion at all (opentr.sh prints one for Grafana's password).
_EXPANSION = re.compile(
    r"(?<!\\)\$\{[#!]?([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}" r"|(?<!\\)\$([A-Za-z_][A-Za-z0-9_]*)"
)

# What follows the name inside ${...} when the expansion is safe under `set -u`:
# :- :=  :+  :?  and their colonless forms, optionally after an array subscript.
# NOT ${FOO#x} / ${FOO%x} / ${FOO//a/b} / ${FOO[@]} — those still abort.
_GUARDED_SUFFIX = re.compile(r"^(?:\[[^\]]*\])?:?[-=+?]")


def _assigned_names(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        for pattern in _ASSIGNMENT_PATTERNS:
            for match in pattern.finditer(line):
                names.add(match.group(1))
    return names


def _unguarded_expansions(text: str) -> dict[str, list[tuple[int, str]]]:
    """Expansions that would abort a `set -u` shell if the variable were unset.

    Keyed by variable name; the value lists every (line number, source line) so a
    failure message points at the code rather than just naming the variable.
    """
    assigned = _assigned_names(text)
    found: dict[str, list[tuple[int, str]]] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for match in _EXPANSION.finditer(line):
            if match.group(1) is not None:
                name, suffix = match.group(1), match.group(2)
            else:
                name, suffix = match.group(3), ""
            if _GUARDED_SUFFIX.match(suffix):
                continue
            if name in assigned or name in SHELL_PROVIDED:
                continue
            found.setdefault(name, []).append((lineno, line.strip()))
    return found


def _defaults_block_names() -> set[str]:
    """Names opentr.sh pre-seeds with `: "${FOO:=...}"` before anything reads them."""
    return set(re.findall(r'^\s*:\s*"\$\{([A-Za-z_][A-Za-z0-9_]*):=', OPENTR.read_text(), re.M))


def test_no_unguarded_expansion_is_missing_from_the_defaults_block():
    """The contract between the defaults block and its consumers, enforced.

    Checked per file rather than across both: `common.sh` documents itself as
    sourced into a `set -u` shell, and a variable that only `opentr.sh` happens
    to assign today is one refactor away from being read before it is set.
    """
    defaults = _defaults_block_names()
    offenders: dict[str, dict[str, list[tuple[int, str]]]] = {}

    for path in (OPENTR, COMMON):
        missing = {
            name: sites
            for name, sites in _unguarded_expansions(path.read_text()).items()
            if name not in defaults
        }
        if missing:
            offenders[path.name] = missing

    assert not offenders, (
        "variable expansions that abort `./opentr.sh` under `set -u` when the "
        "variable is unset (i.e. on any checkout without a .env), and that the "
        "opentr.sh defaults block does not pre-seed:\n"
        + "\n".join(
            f"  {filename}:{lineno}: {name}\n      {line}"
            for filename, names in sorted(offenders.items())
            for name, sites in sorted(names.items())
            for lineno, line in sites
        )
        + '\n\nFix the consumer with ${VAR:-} AND add `: "${VAR:=}"` to the '
        "defaults block in opentr.sh."
    )


def test_the_scanner_flags_a_planted_unguarded_expansion():
    """Guard the guard: a scanner that matches nothing reads exactly like a pass.

    Mirrors the real defect — a bare `[ -n "$FOO" ]` on a variable the file never
    assigns — and asserts the detector fires on it.
    """
    planted = '#!/bin/bash\nset -u\nif [ -n "$SOME_UNSET_THING" ]; then echo hi; fi\n'
    assert "SOME_UNSET_THING" in _unguarded_expansions(planted)


@pytest.mark.parametrize(
    "snippet",
    [
        'echo "${SOME_UNSET_THING:-}"',  # colon-dash default
        'echo "${SOME_UNSET_THING:=fallback}"',  # colon-equals assigns
        'echo "${SOME_UNSET_THING:+set}"',  # colon-plus alternate
        'echo "${SOME_UNSET_THING-}"',  # colonless dash
        'SOME_UNSET_THING="x"; echo "$SOME_UNSET_THING"',  # assigned locally
        'echo "\\$SOME_UNSET_THING"',  # escaped: literal text, not an expansion
        '# echo "$SOME_UNSET_THING"',  # comment
    ],
)
def test_the_scanner_does_not_flag_safe_forms(snippet):
    """The other half of guarding the guard: no false positives on safe syntax."""
    assert "SOME_UNSET_THING" not in _unguarded_expansions(snippet)


def test_the_defaults_block_is_found_at_all():
    """If this regex ever stops matching, the test above passes vacuously."""
    names = _defaults_block_names()
    assert len(names) >= 5, f"defaults block not parsed out of opentr.sh: {names}"


def _write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)


def test_ensure_opensearch_models_survives_an_absent_env(tmp_path):
    """Run the real function under `set -u` with no .env — the original crash.

    Sandboxed so it is hermetic: a scratch cwd with no `.env`, an empty model
    cache (so the early "models already present" return does not short-circuit
    the GPU branch we care about), and stub `docker`/`nvidia-smi` on PATH so the
    GPU branch is taken without touching a real daemon or GPU.

    `ensure_opensearch_models` returning 1 is the *expected* outcome here — the
    stub downloads nothing. The assertion is on the shell not aborting: before
    the fix this run died at `[ -n "$GPU_DEVICE_ID" ]`.
    """
    sandbox = tmp_path / "checkout"
    (sandbox / "scripts").mkdir(parents=True)
    (sandbox / "scripts" / "common.sh").write_text(COMMON.read_text())
    # ensure_opensearch_models bails early unless the downloader is present.
    (sandbox / "scripts" / "download-models.py").write_text("# stub\n")
    assert not (sandbox / ".env").exists()

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # `nvidia-smi` succeeding is what selects the GPU branch containing the bug.
    _write_stub(stubs / "nvidia-smi", "exit 0")
    _write_stub(stubs / "docker", "exit 0")

    proc = subprocess.run(
        ["bash", "-c", "set -uo pipefail; source ./scripts/common.sh; ensure_opensearch_models"],
        cwd=sandbox,
        capture_output=True,
        text=True,
        env={"PATH": f"{stubs}:{os.environ['PATH']}"},
        timeout=120,
    )
    combined = proc.stdout + proc.stderr

    assert "unbound variable" not in combined, (
        "sourcing scripts/common.sh into a `set -u` shell with no .env aborted on "
        f"an unbound variable:\n{combined}"
    )
    # Proof the run actually reached the download attempt rather than failing
    # earlier for an unrelated reason — otherwise the assertion above is vacuous.
    assert "Downloading OpenSearch neural model" in combined, combined
