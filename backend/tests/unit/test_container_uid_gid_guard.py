"""No shell script may hardcode ``1000:1000`` as the container user's ownership.

``backend/Dockerfile.prod`` creates the non-root user with ``useradd -u 1000`` (UID pinned)
but ``groupadd -r appuser`` — a *system* group with no GID pin, which lands on whatever the
next free system GID is. In the built image that is **999**, verified live::

    $ docker exec opentranscribe-backend id appuser
    uid=1000(appuser) gid=999(appuser) groups=999(appuser),44(video)

Seven shell scripts nevertheless chowned model caches, the ``pipeline_scratch`` volume and the
watch folder to ``1000:1000``, setting a group that does not exist inside the image, so a path
repaired by a script diverged from one created by the image itself (issue #580). Today only the
*owner* bits are load-bearing, so those chowns were cosmetically wrong rather than broken — but
that is a property of the current mounts, not a guarantee, and it is exactly the kind of drift
that is invisible until a path needs group access.

This is a **static** test: it reads the scripts, it does not run them, so it costs milliseconds.
It is deliberately narrow — it proves no ownership operation hardcodes the wrong GID, not that
``CONTAINER_UID_GID`` is expanded correctly at runtime.

**Guard the guard.** A scanner that matches nothing passes everything (this repo has shipped
that failure twice). The must-fire and must-stay-clean cases below each encode a shape that a
draft of this scanner got wrong: a ``1000:1000`` inside a ``#`` comment explaining *why* it is
wrong, and a ``1000:1000`` that is not an ownership operation at all.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The correct ownership for any path the backend/worker containers own.
_EXPECTED = "1000:999"

#: Lines that both mention ``1000:1000`` and perform (or print) an ownership change.
_OWNERSHIP_RE = re.compile(r"\b(chown|chgrp|--chown)\b[^\n]*\b1000:1000\b")

#: The shape issue #580 actually shipped: a variable DEFAULT, with no chown on the line.
#: `_OWNERSHIP_RE` requires a chown/chgrp/--chown token, so it scored the real offending
#: line -- `UID_GID="${SHARED_VOLUME_OWNER:-1000:1000}"` -- clean. Recovered from git:
#:   git log -p --follow -- scripts/fix-shared-volume-perms.sh
_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?\w*(?:UID|GID|OWNER)\w*\s*=[^\n]*\b1000:1000\b")

#: ``<relative path>:<line number>`` -> written reason. Empty on purpose: every offender was
#: fixed rather than excused. An entry here needs a real reason, not "legacy".
_ALLOWLIST: dict[str, str] = {}


def _shell_scripts() -> list[Path]:
    """Git-TRACKED ``*.sh`` only.

    A plain ``rglob`` also walks ``.claude/worktrees/*`` (full checkouts of other branches)
    and ``OT_TEST/`` (a release rehearsal's downloaded copy of a *published* release), and
    reported both as offenders of this branch — code this branch cannot fix.
    """
    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", "--", "*.sh"],
        capture_output=True,
        check=True,
    )
    return sorted(_REPO_ROOT / rel for rel in out.stdout.decode().split("\0") if rel)


def _scan_text(text: str) -> list[int]:
    """Return 1-based line numbers of hardcoded-``1000:1000`` ownership operations."""
    offenders = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue  # a comment explaining the pitfall is not the pitfall
        if _OWNERSHIP_RE.search(line) or _ASSIGNMENT_RE.search(line):
            offenders.append(lineno)
    return offenders


def _scan_repo() -> list[str]:
    keys = []
    for path in _shell_scripts():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for lineno in _scan_text(path.read_text(encoding="utf-8", errors="replace")):
            keys.append(f"{rel}:{lineno}")
    return keys


def test_no_shell_script_hardcodes_the_wrong_container_gid() -> None:
    offenders = [key for key in _scan_repo() if key not in _ALLOWLIST]
    assert not offenders, (
        "These lines chown to 1000:1000, but the container user is "
        f"{_EXPECTED} (appuser: useradd -u 1000 + groupadd -r). Use "
        '"$CONTAINER_UID_GID" (scripts/common.sh) or "${CONTAINER_UID_GID:-1000:999}" '
        f"in a standalone script:\n  " + "\n  ".join(offenders)
    )


def test_no_stale_allowlist_entries() -> None:
    live = set(_scan_repo())
    stale = sorted(set(_ALLOWLIST) - live)
    assert not stale, f"Allowlist entries no longer match any line: {stale}"


def test_allowlist_entries_carry_a_written_reason() -> None:
    thin = sorted(key for key, reason in _ALLOWLIST.items() if len(reason.strip()) < 40)
    assert not thin, f"Allowlist entries need a real written reason: {thin}"


@pytest.mark.parametrize(
    "label,line",
    [
        ("plain chown", 'chown -R 1000:1000 "$DIR"'),
        ("sudo chown", "sudo chown -R 1000:1000 /models"),
        ("inside docker run", 'docker run --rm busybox sh -c "chown -R 1000:1000 /scratch"'),
        ("printed instruction", 'echo "  sudo chown -R 1000:1000 $DIR"'),
        ("dockerfile-style flag", "COPY --chown=1000:1000 . ."),
        (
            "variable default, the #580 shape",
            'UID_GID="${SHARED_VOLUME_OWNER:-1000:1000}"',
        ),
        (
            "variable default with an already-suffixed name",
            'CONTAINER_UID_GID="${CONTAINER_UID_GID:-1000:1000}"',
        ),
        ("bare assignment", "SHARED_VOLUME_OWNER=1000:1000"),
        ("exported assignment", "export CONTAINER_UID_GID=1000:1000"),
    ],
)
def test_scanner_fires_on_offending_shapes(label: str, line: str) -> None:
    assert _scan_text(line) == [1], f"scanner missed {label}"


@pytest.mark.parametrize(
    "label,line",
    [
        ("comment explaining the trap", "# a chown to 1000:1000 sets a nonexistent group"),
        ("indented comment", "    # chown -R 1000:1000 was wrong (issue #580)"),
        ("corrected ownership", 'chown -R "$CONTAINER_UID_GID" "$DIR"'),
        ("explicit correct pair", "chown -R 1000:999 /models"),
        ("unrelated 1000:1000", 'echo "port map 1000:1000"'),
        ("unrelated assignment, not UID/GID/OWNER", "PORT_MAP=1000:1000"),
        ("corrected variable default", 'UID_GID="${SHARED_VOLUME_OWNER:-1000:999}"'),
        (
            "comment mentioning the old default in an assignment shape",
            "# default used to be 1000:1000 (issue #580)",
        ),
    ],
)
def test_scanner_stays_silent_on_clean_shapes(label: str, line: str) -> None:
    assert _scan_text(line) == [], f"scanner false-positived on {label}"


#: Every site that hardcodes the corrected default, and the exact string it must contain.
#: Widened from a single common.sh check (issue #602) so a regression in any ONE of the
#: four standalone-default sites can't hide behind the other three staying correct.
_DEFAULT_SITES = {
    "scripts/common.sh": 'CONTAINER_UID_GID="${CONTAINER_UID_GID:-1000:999}"',
    "opentranscribe.sh": 'CONTAINER_UID_GID="${CONTAINER_UID_GID:-1000:999}"',
    "scripts/fix-model-permissions.sh": 'CONTAINER_UID_GID="${CONTAINER_UID_GID:-1000:999}"',
    "scripts/fix-shared-volume-perms.sh": 'UID_GID="${SHARED_VOLUME_OWNER:-1000:999}"',
}


def test_scanner_actually_reads_the_real_scripts() -> None:
    """A scan of zero files would report a clean repo forever."""
    scripts = _shell_scripts()
    assert len(scripts) > 10, f"expected the repo's shell scripts, found {len(scripts)}"
    assert any(p.name == "common.sh" for p in scripts)
    # And the corrected constant is actually present where the fix lives, at every site.
    for rel, expected in _DEFAULT_SITES.items():
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        assert expected in text, f"{rel} is missing the corrected default: {expected!r}"
