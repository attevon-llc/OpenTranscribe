"""Pins the fix for issue #454: subdirectory-conftest fixtures must stay visible.

Under pytest >= 9.1, ``Session.collect`` rebuilds a directory's child collectors
whenever a command-line argument *ends* at a file inside that directory, while
``FixtureManager`` binds a conftest's fixtures to the **first** collector object
and registers them exactly once. A selection that leaves a subdirectory and
comes back::

    pytest tests/unit/test_a.py tests/test_b.py tests/unit/test_c.py

therefore hangs ``test_c.py`` off a second, unregistered ``Package`` object, and
every fixture in ``tests/unit/conftest.py`` disappears from it.

**Those tests ERROR at setup — they do not fail.** An error is not a failed
assertion, so a run in that shape silently stops exercising whatever the missing
fixtures cover (here: the fail-closed-environment and Celery-reliability guards)
while still looking green-ish. That is the exact failure mode issue #431 exists
to prevent, which is why this is pinned rather than left to convention.

The workaround lives in ``tests/fixtures/dir_collector_memo.py``; read its
docstring for the mechanism and the pytest version bisect.

Five tests, deliberately layered:

* two run the **real** repository tree, so deleting the workaround from the root
  conftest's ``pytest_plugins`` breaks them (verified: they fail with 3 and 10
  setup errors respectively when it is unregistered);
* ``test_the_hazard_still_exists_in_this_pytest`` is the **must-fire control** —
  it proves the installed pytest genuinely has the bug, so the two above are not
  passing for free. It skips (with removal instructions) once upstream fixes it;
* ``test_the_memo_plugin_neutralises_the_hazard`` runs the same synthetic tree
  with the real plugin module loaded, isolating cause from effect;
* ``test_the_pinned_selection_really_spans_a_subdirectory_conftest`` guards the
  guard: it fails if the selections stop straddling a subdirectory conftest, so
  the two real-tree tests cannot quietly degenerate into checking nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Keeps the whole module on ONE xdist worker so the module-scoped
# `mixed_selection_run` child process is spawned once. Under `--dist loadgroup`
# the two tests that share it otherwise land on different workers and each pays
# the ~16 s nested run — measured, both showing up in `--durations`.
pytestmark = pytest.mark.xdist_group("conftest_fixture_visibility")

#: backend/ — cwd for the real-tree child runs, and the import root.
BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Substring pytest prints when a requested fixture cannot be resolved.
FIXTURE_LOOKUP_FAILURE = "not found"

#: The repository's own conftest plugin under test.
MEMO_PLUGIN = "fixtures.dir_collector_memo"


def _child_env(**extra: str) -> dict[str, str]:
    """Environment for a nested pytest, scrubbed of the parent run's state.

    ``PYTEST_ADDOPTS`` (set by ``scripts/run-backend-tests.sh``), the xdist
    worker id and pytest-cov's subprocess hooks would all leak into the child
    and change what it collects, which is the one thing these tests measure.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_CORE_"))
    }
    env.update(extra)
    return env


def _run_pytest(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run pytest in a child process and return the completed process."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "pytest", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        check=False,
        timeout=600,
    )


def _assert_no_fixture_lookup_errors(result: subprocess.CompletedProcess) -> None:
    """Fail with the child's own output if any fixture failed to resolve."""
    offenders = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if "fixture '" in line and FIXTURE_LOOKUP_FAILURE in line
    ]
    assert offenders == [], (
        "a subdirectory conftest's fixtures went missing in a mixed selection "
        "(issue #454):\n" + "\n".join(offenders) + f"\n--- stdout ---\n{result.stdout}"
    )


# The reported reproduction, widened to straddle BOTH subdirectory conftests in
# one child process. The shape that matters is: an argument in a subdirectory,
# then an argument ending at a file **directly under tests/** (that is what makes
# pytest rebuild `Dir(tests)`'s children), then another argument back in the same
# subdirectory. `tests/unit` and `tests/api` each bracket the middle argument.
#
# The middle argument is a database-free module on purpose: this must fail for
# the collection bug and for nothing else.
#
# `tests/api/` has NO `__init__.py` while `tests/unit/` does, and both break
# identically — half the evidence that the packaging asymmetry was never the
# cause.
MIXED_SELECTION = [
    "tests/api/test_user_settings.py",
    "tests/unit/test_ddl_marker_discipline.py",
    "tests/test_error_classification.py",
    "tests/unit/test_celery_reliability.py",
    "tests/api/test_groups_tenancy.py",
]

#: Path index of the argument that must sit directly under `tests/`.
MIXED_PIVOT = 2

# `-k` deselects AFTER collection, and collection is where the hazard forms — the
# duplicate collector and the fixture closure are both built before any filtering
# happens. Narrowing to the tests that actually request a subdirectory fixture
# therefore costs nothing in detection power. It matters: ~10 s of every nested
# run is fixed cost (interpreter + root conftest + `app.main`), and before this
# these were the two slowest tests in the whole backend suite at 14 s and 41 s.
UNIT_FIXTURE_TESTS = [
    "tests/unit/test_celery_reliability.py::test_ssl_options_are_set_for_rediss",
    "tests/unit/test_celery_reliability.py::test_gate_is_env_driven[true]",
    "tests/unit/test_celery_reliability.py::test_gate_is_env_driven[false]",
]
API_FIXTURE_TEST = (
    "tests/api/test_groups_tenancy.py::test_create_group_stamps_the_active_organization"
)
WANTED = (
    "test_ssl_options_are_set_for_rediss or test_gate_is_env_driven "
    "or test_create_group_stamps_the_active_organization"
)


@pytest.fixture(scope="module")
def mixed_selection_run() -> subprocess.CompletedProcess:
    """One nested pytest over `MIXED_SELECTION`, shared by the two tests below.

    Module-scoped because the ~10 s fixed startup cost would otherwise be paid
    twice to assert two things about the same run.

    `-rA` rather than `-v`: pyproject's addopts already carry `-q`, the two
    verbosity flags cancel out, and there would be no per-test PASSED lines left
    to assert on — which is how the first version of this test passed while
    checking only the absence of an error string.
    """
    return _run_pytest(
        [
            *MIXED_SELECTION,
            "-k",
            WANTED,
            "-p",
            "no:randomly",
            "-n0",
            "-m",
            "",
            "-rA",
            "-W",
            "ignore",
        ],
        cwd=BACKEND_ROOT,
        env=_child_env(),
    )


def test_the_issue_454_command_line_runs_clean(mixed_selection_run):
    """`tests/unit/conftest.py`'s `run_in_clean_process` survives the selection."""
    _assert_no_fixture_lookup_errors(mixed_selection_run)
    assert mixed_selection_run.returncode == 0, (
        f"nested pytest exited {mixed_selection_run.returncode}"
        f"\n--- stdout ---\n{mixed_selection_run.stdout}"
        f"\n--- stderr ---\n{mixed_selection_run.stderr}"
    )
    # Positive evidence, not just the absence of an error string: the tests that
    # request the subdirectory fixture must have actually run and passed.
    for nodeid in UNIT_FIXTURE_TESTS:
        assert f"PASSED {nodeid}" in mixed_selection_run.stdout, (
            f"{nodeid} did not run and pass\n--- stdout ---\n{mixed_selection_run.stdout}"
        )


def test_a_second_subdirectory_conftest_survives_the_same_shape(mixed_selection_run):
    """`tests/api/conftest.py`'s `org_context` survives it too.

    Generality check. The bug is a property of the collection tree, not of one
    file, so a fix that only rescued ``tests/unit`` would be the wrong fix.
    """
    assert f"PASSED {API_FIXTURE_TEST}" in mixed_selection_run.stdout, (
        f"{API_FIXTURE_TEST} did not run and pass — it is the only test in this "
        f"selection that requests org_context"
        f"\n--- stdout ---\n{mixed_selection_run.stdout}"
    )


def _write_hazard_tree(root: Path) -> None:
    """Lay out the smallest tree that reproduces the collection hazard.

    ``suite/sub/`` owns a conftest fixture; ``suite/`` owns a plain test file.
    Naming an argument in ``suite/`` between two in ``suite/sub/`` is what makes
    pytest rebuild ``suite/sub``'s collector.
    """
    (root / "pytest.ini").write_text("[pytest]\n")
    sub = root / "suite" / "sub"
    sub.mkdir(parents=True)
    (root / "suite" / "test_top.py").write_text("def test_top():\n    assert True\n")
    (sub / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef sub_only():\n    return 'present'\n"
    )
    (sub / "test_first.py").write_text("def test_first():\n    assert True\n")
    (sub / "test_second.py").write_text(
        "def test_second(sub_only):\n    assert sub_only == 'present'\n"
    )


#: Argument order that leaves `suite/sub`, visits `suite`, and returns.
HAZARD_ARGS = [
    "suite/sub/test_first.py",
    "suite/test_top.py",
    "suite/sub/test_second.py",
]


def test_the_hazard_still_exists_in_this_pytest(tmp_path):
    """Must-fire control: bare pytest still loses the fixture without the memo.

    Without this, the two real-tree tests above could pass on a pytest that
    never had the bug and would prove nothing — a green check measuring nothing,
    which is the specific thing this repo audits for.

    Skips rather than fails when pytest is fixed, because that is good news with
    an action attached, not a regression.
    """
    _write_hazard_tree(tmp_path)
    result = _run_pytest(
        [*HAZARD_ARGS, "-p", "no:cacheprovider", "-p", "no:randomly", "-q"],
        cwd=tmp_path,
        env=_child_env(),
    )
    combined = result.stdout + result.stderr
    if "sub_only" not in combined:
        pytest.skip(
            f"pytest {pytest.__version__} no longer rebuilds directory collectors: "
            "the issue #454 workaround in tests/fixtures/dir_collector_memo.py and its "
            "entry in the root conftest's pytest_plugins can be deleted, along with "
            "this module."
        )
    assert "2 passed, 1 error" in combined, (
        "expected the unpatched hazard tree to lose exactly one fixture\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_the_memo_plugin_neutralises_the_hazard(tmp_path):
    """The same tree, with the repo's real plugin module loaded, collects cleanly.

    ``-p`` loads ``tests/fixtures/dir_collector_memo.py`` itself rather than a
    copy pasted into this test, so the two cannot drift apart.
    """
    _write_hazard_tree(tmp_path)
    result = _run_pytest(
        [*HAZARD_ARGS, "-p", "no:cacheprovider", "-p", "no:randomly", "-p", MEMO_PLUGIN, "-q"],
        cwd=tmp_path,
        env=_child_env(PYTHONPATH=str(BACKEND_ROOT / "tests")),
    )
    _assert_no_fixture_lookup_errors(result)
    assert result.returncode == 0, (
        f"nested pytest exited {result.returncode}\n--- stdout ---\n{result.stdout}"
        f"\n--- stderr ---\n{result.stderr}"
    )
    assert "3 passed" in result.stdout, (
        f"expected all three tests to run\n--- stdout ---\n{result.stdout}"
    )


def test_the_pinned_selection_really_spans_a_subdirectory_conftest():
    """Guard the guard: `MIXED_SELECTION` must still be able to expose the bug.

    Every ingredient here is load-bearing, and losing any one of them turns the
    two tests above into checks that pass while measuring nothing:

    * the fixtures must still live in a **subdirectory** conftest (moving
      ``run_in_clean_process`` into the root ``tests/conftest.py`` would make it
      immune, and the pin meaningless);
    * the tests we assert on must still request them;
    * the pivot argument must sit **directly under** ``tests/``, and each
      subdirectory must have an argument on both sides of it.
    """
    unit_conftest = (BACKEND_ROOT / "tests" / "unit" / "conftest.py").read_text()
    assert "def run_in_clean_process(" in unit_conftest, (
        "run_in_clean_process is no longer defined in tests/unit/conftest.py — "
        "repoint MIXED_SELECTION at a fixture that still lives in a subdirectory "
        "conftest, or delete this module if none do."
    )

    consumer = (BACKEND_ROOT / "tests" / "unit" / "test_celery_reliability.py").read_text()
    assert "run_in_clean_process" in consumer, (
        "tests/unit/test_celery_reliability.py no longer requests the "
        "subdirectory fixture, so MIXED_SELECTION cannot detect the bug."
    )

    api_conftest = (BACKEND_ROOT / "tests" / "api" / "conftest.py").read_text()
    assert "def org_context(" in api_conftest, (
        "tests/api/conftest.py no longer defines org_context, so MIXED_SELECTION "
        "no longer covers a second subdirectory conftest."
    )

    for arg in MIXED_SELECTION:
        assert (BACKEND_ROOT / arg).exists(), f"{arg} no longer exists"

    # The pivot is what forces the re-collection of tests/: it must sit directly
    # under tests/, not in either subdirectory being revisited.
    pivot = Path(MIXED_SELECTION[MIXED_PIVOT])
    assert pivot.parent == Path("tests"), (
        f"{pivot} must live directly under tests/ — an argument ending at a file "
        "there is what makes pytest rebuild the subdirectory collectors"
    )

    # ...and each subdirectory must be named both before and after the pivot,
    # which is what makes its collector get rebuilt *between* two of its own
    # files rather than merely once at the end.
    before = {Path(a).parent for a in MIXED_SELECTION[:MIXED_PIVOT]}
    after = {Path(a).parent for a in MIXED_SELECTION[MIXED_PIVOT + 1 :]}
    for subdir in (Path("tests/unit"), Path("tests/api")):
        assert subdir in before and subdir in after, (
            f"{subdir} must appear both before and after the pivot in "
            f"MIXED_SELECTION; got before={sorted(map(str, before))} "
            f"after={sorted(map(str, after))}"
        )
