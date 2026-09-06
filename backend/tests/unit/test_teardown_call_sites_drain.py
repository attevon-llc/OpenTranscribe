"""Every teardown call site that can reach a CUDA-holding container must drain it first,
carry its own `-t "$OT_STOP_GRACE_GPU"`, or be explicitly allowlisted with a reason
(issue #782).

`docker-compose.yml`'s `stop_grace_period` key is the primary fix (compose bakes it into
`Config.StopTimeout` at container CREATE time, and the daemon honours it even for a bare
`docker stop`), but a container created BEFORE that key existed keeps `StopTimeout=null`
until it is recreated. Every `opentr.sh`/`opentranscribe.sh`/`common.sh`/
`uninstall-offline-package.sh` call site that tears the stack down is the migration
bridge for that window, and this file is the regression guard that a new call site (or an
edit to an existing one) does not silently drop back to docker's bare 10s default.

This is a **scanner**, not a hand-maintained list: it regexes every
`docker compose $VAR (down|stop|restart)`, the `docker compose (-f ...)+ "$@"`
argv-forwarding idiom `stop_all_containers()` uses, every `restart_compose <verb>` call
(the wrapper `opentr.sh`'s restart-* commands go through), and every bare `docker stop` —
across the four shell files — and checks each one is covered by:

1. a direct `-t "$OT_STOP_GRACE_GPU"` on the same (continuation-joined) logical line, or
2. a preceding call to `ot_drain_gpu_workers`/`ot_drain_gpu_workers_by_container` within
   the same enclosing function or `case` arm, or
3. an explicit `_ALLOWLIST` entry with a written reason.

A **stale** allowlist entry (the site it names now drains, by either of the first two
routes) FAILS the test — the list can only shrink, never accumulate exceptions nobody
re-examines. The must-fire control at the bottom proves the scanner does not silently
match nothing, which is how a marker selecting zero tests, or a hygiene check comparing
selectors by string equality, have both shipped clean in this repo before.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

_SCRIPTS = (
    "opentr.sh",
    "opentranscribe.sh",
    "scripts/common.sh",
    "scripts/uninstall-offline-package.sh",
)

_DRAIN_CALL_RE = re.compile(r"\bot_drain_gpu_workers(?:_by_container)?\b")
_DIRECT_TIMEOUT_RE = re.compile(r'-t\s+"?\$\{?OT_STOP_GRACE_GPU\b')

# Boundary lines that close off the backward "same scope" walk: a function's own closing
# brace, the end of a `case` arm, a function opening (walking further back would leave
# this function and enter whatever precedes it), a `case` header, or `esac`.
_SCOPE_BOUNDARY_RES = (
    re.compile(r"^\}$"),
    re.compile(r"^;;$"),
    re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{$"),
    re.compile(r"^case\b"),
    re.compile(r"^esac$"),
)

# The four call-site shapes that can reach a torn-down container.
_PATTERN_A = re.compile(r"docker compose\s+\$\{?[A-Za-z_]\w*\}?\s+(down|stop|restart)\b")
_PATTERN_B = re.compile(r'docker compose\s+(?:-f\s+\S+\s+)+"\$@"')
_PATTERN_C = re.compile(r"\brestart_compose\s+(restart|down|stop)\b")
_PATTERN_D = re.compile(r"\bdocker\s+stop\b")

# ---------------------------------------------------------------------------------------
# Allowlist. `<file>::<normalized-line>  ->  written reason`. Keyed by content, not line
# number, so the entry naturally goes stale (and fails) the moment the line changes.
# ---------------------------------------------------------------------------------------
_ALLOWLIST: dict[str, str] = {
    "opentr.sh::restart_compose restart frontend || rc=$?": (
        "the frontend service holds no CUDA context -- restart_frontend() is "
        "deliberately unchanged (issue #782 plan, B4 call-site table)"
    ),
}


def _join_continuations(text: str) -> list[tuple[str, int]]:
    """Collapse backslash-newline continuations into one logical line each, paired with
    the ORIGINAL line number of the first physical line (for error messages). A
    multi-line `restart_compose restart backend \\\n  celery-worker \\\n  ...` call must
    be seen by the pattern matchers as a single line, or `-t "$OT_STOP_GRACE_GPU"` added
    to the first line is invisible to a check on any other physical line."""
    raw_lines = text.split("\n")
    logical: list[tuple[str, int]] = []
    buf: list[str] = []
    start_line = 1
    for i, line in enumerate(raw_lines, start=1):
        if not buf:
            start_line = i
        stripped = line.rstrip("\n")
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(stripped)
        logical.append((" ".join(b.strip() for b in buf), start_line))
        buf = []
    if buf:
        logical.append((" ".join(b.strip() for b in buf), start_line))
    return logical


def _is_boundary(stripped_line: str) -> bool:
    return any(p.match(stripped_line) for p in _SCOPE_BOUNDARY_RES)


@dataclass(frozen=True)
class _Site:
    file: str
    lineno: int
    text: str

    @property
    def key(self) -> str:
        return f"{self.file}::{self.text}"


def _find_sites(file_label: str, logical_lines: list[tuple[str, int]]) -> list[_Site]:
    sites: list[_Site] = []
    for text, lineno in logical_lines:
        stripped = text.strip()
        if not stripped or stripped.startswith(("#", "echo")):
            continue
        if (
            _PATTERN_A.search(stripped)
            or _PATTERN_B.search(stripped)
            or _PATTERN_C.search(stripped)
            or _PATTERN_D.search(stripped)
        ):
            sites.append(_Site(file_label, lineno, stripped))
    return sites


def _is_covered(file_label: str, all_stripped: list[str], idx: int) -> bool:
    """Is the site at `all_stripped[idx]` covered by a direct -t, or a drain call earlier
    in the same enclosing function/case-arm?"""
    if _DIRECT_TIMEOUT_RE.search(all_stripped[idx]):
        return True
    j = idx - 1
    while j >= 0:
        candidate = all_stripped[j]
        if _DRAIN_CALL_RE.search(candidate):
            return True
        if _is_boundary(candidate):
            break
        j -= 1
    return False


def _scan_file(relative_path: str) -> tuple[list[_Site], list[str]]:
    """Returns (all call sites, all_stripped_logical_lines) for coverage lookups."""
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    logical = _join_continuations(text)
    stripped = [t.strip() for t, _ in logical]
    sites = _find_sites(relative_path, logical)
    return sites, stripped


def _all_offenders() -> list[_Site]:
    offenders: list[_Site] = []
    for relative_path in _SCRIPTS:
        path = REPO_ROOT / relative_path
        if not path.exists():
            continue
        sites, stripped = _scan_file(relative_path)
        stripped_only = [s for s in stripped]
        # Re-derive each site's index in `stripped` by matching text+order (a given
        # stripped line can repeat, e.g. two `restart_compose ps` lines -- track a
        # cursor rather than `list.index`, which would always find the first).
        cursor = 0
        for site in sites:
            while cursor < len(stripped_only) and stripped_only[cursor] != site.text:
                cursor += 1
            if cursor >= len(stripped_only):
                # Should not happen: every site came from this same stripped list.
                offenders.append(site)
                continue
            if not _is_covered(relative_path, stripped_only, cursor):
                offenders.append(site)
            cursor += 1
    return offenders


@pytest.mark.unit
def test_every_teardown_call_site_drains_or_is_allowlisted():
    offenders = [s for s in _all_offenders() if s.key not in _ALLOWLIST]
    detail = "\n".join(f"  {s.file}:{s.lineno}  {s.text}" for s in offenders)
    assert not offenders, (
        f"{len(offenders)} teardown call site(s) reach a CUDA-holding container with "
        f'no drain, no -t "$OT_STOP_GRACE_GPU", and no allowlist entry:\n{detail}\n'
        "Fix: call ot_drain_gpu_workers()/ot_drain_gpu_workers_by_container() earlier in "
        'the same function or case arm, add -t "$OT_STOP_GRACE_GPU" to the call itself, '
        "or add a reasoned _ALLOWLIST entry if the site genuinely holds no CUDA context."
    )


@pytest.mark.unit
def test_no_stale_allowlist_entries():
    live_keys = {s.key for s in _all_offenders()}
    stale = sorted(set(_ALLOWLIST) - live_keys)
    assert not stale, (
        f"{len(stale)} allowlist entry(ies) no longer match an uncovered call site -- "
        f"the site now drains (or the line changed), so delete the entry: {stale}"
    )


@pytest.mark.unit
def test_allowlist_entries_carry_a_written_reason():
    thin = sorted(key for key, reason in _ALLOWLIST.items() if len(reason.strip()) < 20)
    assert not thin, f"allowlist entries with no substantive reason: {thin}"


# -----------------------------------------------------------------------------------
# Guard the guard: a scanner that matches nothing reads exactly like a clean tree.
# -----------------------------------------------------------------------------------


def _run_scan_on_synthetic_script(body: str) -> list[_Site]:
    logical = _join_continuations(body)
    stripped = [t.strip() for t, _ in logical]
    sites = _find_sites("synthetic.sh", logical)
    offenders = []
    cursor = 0
    for site in sites:
        while cursor < len(stripped) and stripped[cursor] != site.text:
            cursor += 1
        if not _is_covered("synthetic.sh", stripped, cursor):
            offenders.append(site)
        cursor += 1
    return offenders


@pytest.mark.unit
def test_scanner_fires_on_a_synthetic_undrained_teardown():
    """The must-fire control: without this, a scanner that matches nothing would report
    the real tree clean for the wrong reason."""
    synthetic = (
        "some_func() {\n"
        "  echo hi\n"
        "  # shellcheck disable=SC2086\n"
        "  docker compose $chain down --remove-orphans\n"
        "}\n"
    )
    offenders = _run_scan_on_synthetic_script(synthetic)
    assert len(offenders) == 1, f"expected exactly one offender, got {offenders}"
    assert "docker compose $chain down" in offenders[0].text


@pytest.mark.unit
def test_scanner_stays_clean_when_a_drain_call_precedes_the_teardown():
    synthetic = (
        "some_func() {\n"
        '  ot_drain_gpu_workers "$chain"\n'
        "  docker compose $chain down --remove-orphans\n"
        "}\n"
    )
    assert _run_scan_on_synthetic_script(synthetic) == []


@pytest.mark.unit
def test_scanner_stays_clean_with_a_direct_timeout_flag():
    synthetic = 'some_func() {\n  docker compose $chain stop -t "$OT_STOP_GRACE_GPU" foo\n}\n'
    assert _run_scan_on_synthetic_script(synthetic) == []


@pytest.mark.unit
def test_scanner_does_not_let_a_drain_call_leak_across_a_function_boundary():
    """A drain call in a DIFFERENT function must not count as coverage for this one."""
    synthetic = (
        "other_func() {\n"
        '  ot_drain_gpu_workers "$chain"\n'
        "}\n"
        "some_func() {\n"
        "  docker compose $chain down\n"
        "}\n"
    )
    offenders = _run_scan_on_synthetic_script(synthetic)
    assert len(offenders) == 1, (
        f"a drain call in a sibling function was treated as covering this one: {offenders}"
    )


@pytest.mark.unit
def test_scanner_does_not_let_a_drain_call_leak_across_a_case_arm_boundary():
    synthetic = (
        'case "$1" in\n'
        "  start)\n"
        '    ot_drain_gpu_workers "$chain"\n'
        "    ;;\n"
        "  stop)\n"
        "    docker compose $chain down\n"
        "    ;;\n"
        "esac\n"
    )
    offenders = _run_scan_on_synthetic_script(synthetic)
    assert len(offenders) == 1, (
        f"a drain call in a sibling case arm was treated as covering this one: {offenders}"
    )


@pytest.mark.unit
def test_scanner_covers_a_case_arm_when_the_drain_call_is_in_the_same_arm():
    synthetic = (
        'case "$1" in\n'
        "  stop)\n"
        '    ot_drain_gpu_workers "$chain"\n'
        "    docker compose $chain down\n"
        "    ;;\n"
        "esac\n"
    )
    assert _run_scan_on_synthetic_script(synthetic) == []


@pytest.mark.unit
def test_scanner_recognizes_the_argv_forwarding_idiom():
    """stop_all_containers()'s two chain calls forward "$@" rather than a literal verb --
    pattern A alone would miss them entirely."""
    synthetic = (
        "stop_all_containers() {\n"
        "  docker compose -f docker-compose.yml \\\n"
        '    -f docker-compose.override.yml "$@" 2>/dev/null || true\n'
        "}\n"
    )
    offenders = _run_scan_on_synthetic_script(synthetic)
    assert len(offenders) == 1
    assert '"$@"' in offenders[0].text


@pytest.mark.unit
def test_scanner_recognizes_the_restart_compose_wrapper():
    synthetic = "restart_backend() {\n  restart_compose restart backend celery-worker\n}\n"
    offenders = _run_scan_on_synthetic_script(synthetic)
    assert len(offenders) == 1
    assert "restart_compose restart" in offenders[0].text


@pytest.mark.unit
def test_scanner_recognizes_a_bare_docker_stop():
    synthetic = 'some_func() {\n  docker stop "$container" 2>/dev/null || true\n}\n'
    offenders = _run_scan_on_synthetic_script(synthetic)
    assert len(offenders) == 1


@pytest.mark.unit
def test_scanner_ignores_an_echoed_instructional_line():
    """opentr.sh prints `Would run: docker compose \\$COMPOSE_FILES down -v` in its
    --dry-run path -- prose, not an invocation. Must not be flagged."""
    synthetic = 'some_func() {\n  echo "   Would run: docker compose \\$COMPOSE_FILES down -v"\n}\n'
    assert _run_scan_on_synthetic_script(synthetic) == []


@pytest.mark.unit
def test_scanner_does_not_flag_a_multiline_call_with_the_timeout_on_the_first_line():
    """The continuation-joining is what makes this pass: -t is on line 2, the verb is on
    line 1, and only the JOINED logical line contains both."""
    synthetic = (
        "restart_backend() {\n"
        '  restart_compose restart -t "$OT_STOP_GRACE_GPU" backend \\\n'
        "    celery-worker \\\n"
        "    flower || rc=$?\n"
        "}\n"
    )
    assert _run_scan_on_synthetic_script(synthetic) == []


@pytest.mark.unit
def test_scanner_flags_ps_only_when_a_teardown_verb_is_present():
    """A read-only `restart_compose ps` (used for status display after every real
    restart) must never be flagged -- it cannot reach a live CUDA context."""
    synthetic = "some_func() {\n  restart_compose ps\n}\n"
    assert _run_scan_on_synthetic_script(synthetic) == []


# -----------------------------------------------------------------------------------
# Prove the corpus is actually being read (a path typo would make every test above pass
# vacuously against zero call sites).
# -----------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_real_scripts_contain_a_meaningful_number_of_call_sites():
    total = sum(len(_scan_file(f)[0]) for f in _SCRIPTS if (REPO_ROOT / f).exists())
    assert total >= 10, (
        f"only {total} teardown call site(s) found across {_SCRIPTS} -- a path typo or a "
        "pattern regression would also produce a low number that still passes every "
        "other assertion in this file"
    )


@pytest.mark.unit
def test_all_four_scripts_are_syntactically_valid_bash():
    """Sanity check the corpus this scanner reads is real, parseable bash -- a corrupted
    file would produce nonsense call sites."""
    for relative_path in _SCRIPTS:
        path = REPO_ROOT / relative_path
        if not path.exists():
            continue
        proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=15)
        assert proc.returncode == 0, f"{relative_path} failed `bash -n`: {proc.stderr}"
