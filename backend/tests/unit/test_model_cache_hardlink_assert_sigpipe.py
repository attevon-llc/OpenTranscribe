"""``mc_assert_no_hardlinks`` must REPORT when it has something to report.

THE DEFECT THIS PINS
--------------------
``scripts/release-tests/lib/model-cache.sh``'s guard opened with::

    offenders=$(find "$dir" -type f -links +1 2>/dev/null | head -5)

Every caller sources it after ``guardrails.sh``, which sets ``set -euo pipefail``
(``guardrails.sh:19``). ``head -5`` closes the pipe after the fifth path; ``find``
dies of SIGPIPE; ``pipefail`` makes **141** the status of the command substitution;
and ``set -e`` aborts the script **on the assignment** -- two lines before the
``gr_die`` that would have named the cause.

So the guard produced a bare ``exit 141`` with NO OUTPUT AT ALL, in exactly the case
it exists to report. And it exists for a specific incident: nltk >=3.10's ``pathsec``
hardening refuses any ``nltk_data`` file with ``st_nlink > 1``, the rehearsal cache
was seeded by hardlink, **130** files were poisoned, and every transcription in both
scenarios failed with the real cause buried in a database column the harness dropped
on teardown. The whole point of ``mc_assert_no_hardlinks`` is to convert that into an
immediate, named error at seed time -- which it could not do above five offenders.

WHY IT SURVIVED REVIEW, AND WHY THIS FILE EXISTS SEPARATELY
-----------------------------------------------------------
The sibling suite ``test_release_model_cache_pathsec.py`` builds real hardlinked trees
and asserts ``gr_die`` fires. It cannot catch this, for two reasons that both have to
be fixed to see the bug:

* it runs the lib under ``set -uo pipefail`` -- **no** ``-e`` -- so the poisoned
  assignment's 141 is simply discarded and execution continues into ``gr_die``;
* its fixtures build **3** hardlinked files, and the race does not reproduce at that
  size. MEASURED on this host, flat directory: 0/60 bare aborts at 6, 10 and 50
  offenders, and 58/60 at 200. In a NESTED tree shaped like real ``nltk_data``:
  0/40 at 6, 0/40 at 30, and **40/40 at 130** -- the incident's own file count.

That is the same time-not-size lesson the ``| grep -q`` scanners record: ``find``
walking directories has gaps between its writes, and the gaps are what kill it. A
small fixture is not a weaker version of this test, it is a test of something else.

These cases therefore run the REAL shell function under the REAL caller options
(``set -euo pipefail``) against REAL hardlinked trees at the sizes that matter.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODEL_CACHE_LIB = REPO_ROOT / "scripts" / "release-tests" / "lib" / "model-cache.sh"
GUARDRAILS_LIB = REPO_ROOT / "scripts" / "release-tests" / "lib" / "guardrails.sh"

#: ``guardrails.sh`` sets these, and every caller of ``model-cache.sh`` sources it
#: first. Running the lib without ``-e`` is what let the defect through, so the
#: options are asserted against the real file rather than hardcoded in prose.
_CALLER_OPTIONS = "set -euo pipefail"

#: Stubs for the four logging helpers ``model-cache.sh`` depends on. ``gr_die`` must
#: exit non-zero -- that is the behaviour under test -- and it must be DISTINGUISHABLE
#: from the bare abort, which is the entire point: both are non-zero.
_GR_STUBS = (
    "gr_log(){ :; }; "
    'gr_ok(){ echo "OK: $*"; }; '
    'gr_warn(){ echo "WARN: $*" >&2; }; '
    'gr_die(){ echo "DIE: $*" >&2; exit 1; }; '
)

#: Offender counts to exercise. 0 and 1 are the trivial ends; 5 is the old ``head -5``
#: limit exactly; 6 is one past it (the first count that COULD break); 130 is the real
#: nltk incident's file count and the size at which the flat-tree race stops being
#: intermittent and becomes reliable.
_OFFENDER_COUNTS = [0, 1, 5, 6, 130]


def _build_tree(root: Path, n_offenders: int) -> Path:
    """A destination tree with ``n_offenders`` multiply-linked files, plus one clean one.

    Nested rather than flat, because real ``nltk_data`` is (``tokenizers/punkt_tab/<lang>/``)
    and because a flat directory of the same size does NOT reproduce the abort -- ``find``
    emits a flat listing fast enough to beat the reader. Measured: flat 130 files 0/40,
    nested 130 files 40/40.

    The clean file guarantees the tree is never empty, so a "no offenders" pass cannot be
    an artefact of scanning nothing.
    """
    dst = root / "dst" / "nltk_data"
    (dst / "tokenizers").mkdir(parents=True)
    (dst / "clean.tab").write_text("not linked\n", encoding="utf-8")

    src = root / "src" / "nltk_data" / "tokenizers"
    for i in range(n_offenders):
        lang = f"lang{i % 13}"
        (src / "punkt_tab" / lang).mkdir(parents=True, exist_ok=True)
        (dst / "tokenizers" / "punkt_tab" / lang).mkdir(parents=True, exist_ok=True)
        payload = src / "punkt_tab" / lang / f"collocations{i}.tab"
        payload.write_text(f"payload {i}\n", encoding="utf-8")
        os.link(payload, dst / "tokenizers" / "punkt_tab" / lang / f"collocations{i}.tab")
    return dst


def _assert_no_hardlinks(tree: Path) -> subprocess.CompletedProcess[str]:
    """Run the REAL ``mc_assert_no_hardlinks`` under the REAL caller options."""
    script = (
        f"{_CALLER_OPTIONS}\n"
        f"{_GR_STUBS}\n"
        f"source {MODEL_CACHE_LIB}\n"
        f"mc_assert_no_hardlinks '{tree}' 'seeded model cache'\n"
        "echo REACHED_END\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=180, check=False
    )


def test_the_callers_really_do_set_errexit() -> None:
    """The premise of every case below.

    If ``guardrails.sh`` ever stops setting ``-e``, these tests still pass while measuring
    a hazard that no longer exists -- and, worse, the next reviewer concludes the pipe form
    was fine all along.
    """
    text = GUARDRAILS_LIB.read_text(encoding="utf-8")
    assert _CALLER_OPTIONS in text, (
        f"{GUARDRAILS_LIB.name} no longer contains `{_CALLER_OPTIONS}`. model-cache.sh is "
        "sourced after it, so this file's whole premise -- that a SIGPIPE in a command "
        "substitution ABORTS the script -- needs re-deriving before these tests mean anything."
    )


@pytest.mark.parametrize("n_offenders", _OFFENDER_COUNTS)
def test_the_guard_reports_instead_of_aborting_at_every_offender_count(
    n_offenders: int, tmp_path: Path
) -> None:
    """The guard must produce its NAMED error, never a bare exit, at any number of offenders.

    Three things are asserted, and the second is the one the defect broke:

    1. the exit status is ``gr_die``'s 1, not the pipeline's 141;
    2. the message is actually emitted -- a 141 abort printed *nothing whatsoever*, which is
       indistinguishable in a rehearsal log from a phase that simply stopped;
    3. the count is the TRUE total, not the display trim, so 130 poisoned files never reports
       as "5".
    """
    tree = _build_tree(tmp_path, n_offenders)

    proc = _assert_no_hardlinks(tree)
    combined = proc.stdout + proc.stderr

    if n_offenders == 0:
        assert proc.returncode == 0, combined
        assert "REACHED_END" in combined, combined
        assert "no multiply-linked files" in combined, combined
        return

    assert proc.returncode != 141, (
        f"{n_offenders} offender(s): the guard died of SIGPIPE (141) instead of reporting. "
        "That is `find | head -5` under `set -euo pipefail`: the assignment aborts the script "
        "before gr_die can run, so the ONE check that names the nltk pathsec cause produces a "
        f"bare unexplained exit precisely when it has something to say.\n{combined!r}"
    )
    assert proc.returncode == 1, f"{n_offenders} offender(s): expected gr_die's 1\n{combined}"
    assert combined.strip(), (
        f"{n_offenders} offender(s): the guard exited {proc.returncode} and printed NOTHING. "
        "An unexplained exit in a rehearsal log is what cost ten minutes of debugging per run."
    )
    assert "DIE:" in combined, combined
    assert f"{n_offenders} file(s)" in combined, (
        f"the reported count is not the true total ({n_offenders}); it must never be the "
        f"display trim.\n{combined}"
    )
    assert "pathsec" in combined, (
        f"the error no longer names the nltk pathsec cause, which is the only reason this "
        f"guard exists rather than letting the transcriptions fail later.\n{combined}"
    )


def test_the_offender_list_is_still_trimmed_for_display(tmp_path: Path) -> None:
    """Dropping ``head -5`` must not dump the whole tree into the error.

    The fix captures every offender and slices for display. If that slicing were lost, a
    poisoned cache would print thousands of paths and bury its own first line -- so the trim
    is asserted, not assumed, in both directions.
    """
    tree = _build_tree(tmp_path, 130)

    combined = _assert_no_hardlinks(tree).stdout + _assert_no_hardlinks(tree).stderr
    listed = [line for line in combined.splitlines() if "collocations" in line]

    assert listed, f"no offender paths were listed at all:\n{combined}"
    assert len(listed) <= 5 * 2, (  # two invocations above, <=5 paths each
        f"the offender list is no longer trimmed for display -- {len(listed)} paths printed. "
        f"The count line already carries the total.\n{combined}"
    )


#: Attempts allowed when reproducing the race. ⚠️ IT IS A RACE, SO ONE SHOT IS NOT A TEST.
#: A single-attempt version of the control below was **2/12 flaky** under pytest on this host
#: (measured 2026-09-07) even though the standalone harness reproduced 40/40 -- the tree is
#: smaller in the page cache here, so ``find`` sometimes finishes before ``head`` exits. That
#: flakiness is the hazard's own signature, not a defect in the fixture: the same "we ran it
#: and saw nothing" reasoning that licensed the refuted size rule. So the control asserts
#: "reproduces AT LEAST ONCE in N", which is the honest claim, and reports the observed rate
#: when it does not.
_RACE_ATTEMPTS = 25


def test_the_fixture_reproduces_the_abort_against_the_broken_form(tmp_path: Path) -> None:
    """Guard the guard: prove the fixture can still trigger the defect it pins.

    Without this, a fixture that stopped producing enough offenders (or a kernel/coreutils
    change that removed the race) would leave every case above passing while measuring
    nothing -- the "0 findings is indistinguishable from clean" failure this repo has shipped
    twice. So the ORIGINAL broken pipeline is run against the same tree and REQUIRED to abort.

    It is spelled out here rather than reverted in the lib, so nothing in the shared checkout
    is ever mutated to observe a red state.
    """
    tree = _build_tree(tmp_path, 130)
    script = (
        f"{_CALLER_OPTIONS}\n"
        f"offenders=$(find '{tree}' -type f -links +1 2>/dev/null | head -5)\n"
        "echo REACHED_END\n"
    )

    aborts = 0
    for _ in range(_RACE_ATTEMPTS):
        broken = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=180, check=False
        )
        if broken.returncode == 141 and "REACHED_END" not in broken.stdout:
            aborts += 1

    assert aborts > 0, (
        f"the original `find … | head -5` form did not abort ONCE in {_RACE_ATTEMPTS} attempts "
        "against a 130-offender nested tree, so the cases above are not exercising the hazard "
        "on this platform. Re-measure the tree shape and size before concluding the fix is "
        "unnecessary -- and note the refuted premise this file records: a null result over too "
        "few iterations is not evidence of safety. Do not delete these tests."
    )
