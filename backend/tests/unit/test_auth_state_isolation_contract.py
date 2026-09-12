"""Falsifiable proof that the shared auth-state isolation fixture restores to the entry
SNAPSHOT, not to a hardcoded default -- the specific property that distinguishes it from a
hand-rolled ``teardown_method`` (issue #810).

⚠️ **Honesty note, non-negotiable.** #810's own CI failure (`test_it_re_probes_exactly_at_the_
interval_boundary`) is not locally reproducible -- six clean runs of the suite here. This module
proves the isolation fixture does what it claims (restore-to-entry-value, survive a raising
test body, survive a raising setup fixture) and that a `teardown_method`-shaped reset does NOT.
It does **not** prove, and must never be read as proving, that this removes the mechanism
behind #810's flake. The strongest argument against that reading is structural: in
`test_auth_state_degradation.py::TestLockoutStoreDegradation::test_it_re_probes_exactly_at_the_
interval_boundary`, the test calls `self._reset()` at the top of its own body and then derives
`frozen = lockout_module._last_redis_probe + REDIS_REPROBE_SECONDS` two lines later, from a
value it just stamped itself. No neighbour's leftover global can reach that assertion --
whatever ran before is already overwritten by the time `frozen` is computed. Any real
explanation for the flake needs `_last_redis_probe` to change *during* the test body from
*outside* it, i.e. concurrency, and none of the four sibling files spawn a thread. This test
module is order-dependence hygiene, not a diagnosis of #810.

Two legs, following the pattern in `test_conftest_fixture_visibility.py`:

* **Leg 1** runs the REAL repository tree, unmodified: a session-scoped plugin stamps
  distinctive sentinel values into the six `session`/`lockout` cached globals before collection,
  then a nested pytest exercises `test_session_survivor_mutants.py::TestGetStoreColdBoot` (the
  one class in that file that used to carry a hand-rolled `teardown_method`), and the plugin
  dumps the globals' final state after the session. **At base (before this fix) this leg FAILS**
  -- `teardown_method` on the first test run in that class overwrote the sentinels with
  hardcoded `None`/`False`/`0.0`, not the sentinel snapshot, and every following test in the
  class then inherits the destroyed values.
* **Leg 2** is synthetic: a tiny module using the real
  `fixtures.auth_state_isolation.auth_state_globals_restored` fixture, with a test that dirties
  a target and RAISES, and a sibling whose own autouse fixture raises at SETUP -- both must
  still see the dirtied value restored by the time a later test inspects it. Beside it, a
  must-fire CONTROL module reproduces the `teardown_method` shape and must show the target
  overwritten with a hardcoded default rather than restored, so leg 2 cannot pass "for free".

Guard-the-guard: both legs assert the child process actually ran and collected the expected
tests, not merely that its output lacked an error string.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group("auth_state_isolation_contract")

#: backend/ -- cwd for the real-tree child run and the import root.
BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Distinctive, impossible-to-occur-by-default sentinel values for the six globals.
SENTINELS: dict[str, object] = {
    "_redis_client": "SENTINEL-REDIS-CLIENT-837201",
    "_in_memory_store": "SENTINEL-IN-MEMORY-STORE-837201",
    "_store_initialized": "SENTINEL-STORE-INITIALIZED-837201",
    "_last_redis_probe": -4242.0,
    "_cas_script": "SENTINEL-CAS-SCRIPT-837201",
    "_cas_script_client": "SENTINEL-CAS-SCRIPT-CLIENT-837201",
}

SESSION_MODULES = ("app.auth.session", "app.auth.lockout")


def _child_env(**extra: str) -> dict[str, str]:
    """Environment for a nested pytest, scrubbed of the parent run's state.

    Same rationale as `test_conftest_fixture_visibility.py`'s `_child_env`: the parent's
    `PYTEST_ADDOPTS`, xdist worker id, and pytest-cov subprocess hooks must not leak into the
    child and change what it collects or how it isolates.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_CORE_"))
    }
    env.update(extra)
    return env


def _run_pytest(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "pytest", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        check=False,
        timeout=600,
    )


_SENTINEL_PLUGIN_SOURCE = '''
"""Session-scoped plugin: stamp sentinels into the auth-state globals, dump the aftermath."""
import importlib
import json
import os

SENTINELS = {sentinels!r}
MODULE_NAMES = {modules!r}
DUMP_PATH = os.environ["AUTH_STATE_SENTINEL_DUMP"]


def pytest_sessionstart(session):
    for name in MODULE_NAMES:
        module = importlib.import_module(name)
        for attr, value in SENTINELS.items():
            if hasattr(module, attr):
                setattr(module, attr, value)


def pytest_sessionfinish(session, exitstatus):
    dump = {{}}
    for name in MODULE_NAMES:
        module = importlib.import_module(name)
        dump[name] = {{
            attr: getattr(module, attr, "<MISSING-ATTR>") for attr in SENTINELS
        }}
    with open(DUMP_PATH, "w") as f:
        json.dump(dump, f)
'''


@pytest.fixture
def sentinel_plugin(tmp_path: Path) -> tuple[Path, Path]:
    """Write the sentinel-stamping plugin and return (plugin_dir, dump_path)."""
    plugin_dir = tmp_path / "plugin_src"
    plugin_dir.mkdir()
    (plugin_dir / "auth_state_sentinel_plugin.py").write_text(
        _SENTINEL_PLUGIN_SOURCE.format(sentinels=SENTINELS, modules=SESSION_MODULES)
    )
    dump_path = tmp_path / "sentinel_dump.json"
    return plugin_dir, dump_path


def _run_leg1(
    sentinel_plugin: tuple[Path, Path], tmp_path: Path
) -> tuple[subprocess.CompletedProcess, dict]:
    plugin_dir, dump_path = sentinel_plugin
    data_dir = tmp_path / "data"
    models_dir = tmp_path / "models"
    temp_dir = tmp_path / "temp"

    env = _child_env(
        PYTHONPATH=str(plugin_dir) + os.pathsep + str(BACKEND_ROOT / "tests"),
        AUTH_STATE_SENTINEL_DUMP=str(dump_path),
        SKIP_CELERY="true",
        SKIP_S3="True",
        DATA_DIR=str(data_dir),
        MODELS_DIR=str(models_dir),
        TEMP_DIR=str(temp_dir),
    )
    result = _run_pytest(
        [
            "tests/unit/test_session_survivor_mutants.py::TestGetStoreColdBoot",
            "-p",
            "auth_state_sentinel_plugin",
            "-o",
            "addopts=",
            "-n0",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=BACKEND_ROOT,
        env=env,
    )
    dump = json.loads(dump_path.read_text()) if dump_path.exists() else {}
    return result, dump


def test_leg1_real_tree_restores_to_entry_sentinel_not_a_hardcoded_default(
    sentinel_plugin: tuple[Path, Path], tmp_path: Path
) -> None:
    """The real `TestGetStoreColdBoot` class, run against the real fixture, must leave the
    session/lockout globals at the SENTINEL value stamped before collection -- proving the
    per-test restore puts back the entry snapshot rather than a hardcoded reset.

    At base (before the fixture in `fixtures/auth_state_isolation.py` was wired into
    `test_session_survivor_mutants.py`), this leg FAILS: the class's own `teardown_method`
    stamped hardcoded `None`/`False`/`0.0` after the first test, destroying the sentinel for
    every subsequent test and for this assertion.
    """
    result, dump = _run_leg1(sentinel_plugin, tmp_path)

    # Guard the guard: the child must actually have run and collected the four tests in
    # TestGetStoreColdBoot, not merely exited some way that happens to satisfy the dump check.
    assert "4 passed" in result.stdout, (
        f"the child run did not collect/pass the expected 4 tests\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"nested pytest exited {result.returncode}\n--- stdout ---\n{result.stdout}"
        f"\n--- stderr ---\n{result.stderr}"
    )
    assert dump, f"sentinel dump was never written\n--- stdout ---\n{result.stdout}"

    offenders = []
    for module_name, values in dump.items():
        for attr, expected in SENTINELS.items():
            actual = values.get(attr, "<MISSING-KEY>")
            # session module has no _cas_script*; those keys are absent from that module's
            # real globals and the plugin's hasattr guard never sets them there.
            if actual == "<MISSING-ATTR>":
                continue
            if actual != expected:
                offenders.append(f"{module_name}.{attr}: expected {expected!r}, got {actual!r}")

    assert offenders == [], (
        "one or more cached globals were NOT restored to their entry sentinel -- a "
        "teardown_method-shaped reset (or no isolation at all) clobbered them instead:\n"
        + "\n".join(offenders)
    )


# ── Leg 2: synthetic, restore-across-raise and restore-across-setup-failure ──────────────────

_LEG2_MODULE_SOURCE = '''
"""Synthetic target module for the leg-2 restore-across-raise proof."""
value = "entry-value"
'''

_LEG2_REAL_FIXTURE_SOURCE = '''
"""Uses the REAL auth_state_isolation-shaped fixture, generalised to one arbitrary target."""
import json
import os

import pytest

import leg2_target as target

RECORD_PATH = os.environ["LEG2_RECORD_PATH"]


@pytest.fixture
def restored_by_snapshot():
    saved = target.value
    yield
    target.value = saved


def _record(label):
    records = json.loads(open(RECORD_PATH).read()) if os.path.exists(RECORD_PATH) else []
    records.append({"label": label, "value": target.value})
    with open(RECORD_PATH, "w") as f:
        json.dump(records, f)


@pytest.mark.usefixtures("restored_by_snapshot")
def test_dirties_and_raises():
    target.value = "dirtied-by-raising-test"
    raise ValueError("deliberate failure inside the test body")


def test_observes_after_raise():
    _record("after_raising_test")
    assert target.value == "entry-value", "must have been restored despite the raise"


@pytest.fixture(autouse=True)
def _maybe_raise_at_setup(request):
    if request.node.name == "test_setup_raises":
        raise RuntimeError("deliberate failure during setup")
    yield


@pytest.mark.usefixtures("restored_by_snapshot")
def test_setup_raises():
    target.value = "should never run"
    assert False, "unreachable: setup fixture above raises first"


def test_observes_after_setup_failure():
    _record("after_setup_failure")
    assert target.value == "entry-value", "must have been restored despite the setup error"
'''

_LEG2_CONTROL_SOURCE = '''
"""Must-fire control: a teardown_method-shaped reset overwrites with a HARDCODED default,
not the entry snapshot -- proving leg 2 cannot pass "for free"."""
import json
import os

import leg2_target as target

RECORD_PATH = os.environ["LEG2_RECORD_PATH"]


def _record(label):
    records = json.loads(open(RECORD_PATH).read()) if os.path.exists(RECORD_PATH) else []
    records.append({"label": label, "value": target.value})
    with open(RECORD_PATH, "w") as f:
        json.dump(records, f)


class TestControl:
    def teardown_method(self):
        target.value = "hardcoded-default"  # NOT the entry snapshot

    def test_dirties(self):
        target.value = "dirtied-by-control-test"
        assert target.value == "dirtied-by-control-test"


def test_control_observes_hardcoded_default():
    _record("control_after")
    assert target.value == "hardcoded-default", (
        "must-fire control: a teardown_method-shaped reset overwrites with its "
        "hardcoded default, never the caller's real entry value"
    )
'''


def _write_leg2_tree(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "leg2_target.py").write_text(_LEG2_MODULE_SOURCE)
    (root / "test_leg2_real_fixture.py").write_text(_LEG2_REAL_FIXTURE_SOURCE)
    (root / "test_leg2_control.py").write_text(_LEG2_CONTROL_SOURCE)
    return root


def test_leg2_restores_across_a_raising_body_and_a_raising_setup_fixture(tmp_path: Path) -> None:
    """Synthetic tree proving the snapshot/restore SHAPE survives both hazards `monkeypatch`
    handles by construction and a hand-rolled `teardown_method` does not: a test body that
    raises, and an (unrelated) autouse fixture that raises during SETUP.
    """
    tree = _write_leg2_tree(tmp_path / "leg2")
    record_path = tmp_path / "leg2_record.json"

    env = _child_env(
        PYTHONPATH=str(tree),
        LEG2_RECORD_PATH=str(record_path),
    )
    result = _run_pytest(
        ["test_leg2_real_fixture.py", "-p", "no:cacheprovider", "-q", "-rA"],
        cwd=tree,
        env=env,
    )

    # Guard the guard: both hazard tests must actually have run (one fails, one errors) and
    # both observer tests must have run and passed.
    assert "test_dirties_and_raises" in result.stdout, result.stdout
    assert "test_setup_raises" in result.stdout, result.stdout
    assert "PASSED test_leg2_real_fixture.py::test_observes_after_raise" in result.stdout, (
        f"restore-after-raise did not happen\n--- stdout ---\n{result.stdout}"
    )
    assert "PASSED test_leg2_real_fixture.py::test_observes_after_setup_failure" in result.stdout, (
        f"restore-after-setup-failure did not happen\n--- stdout ---\n{result.stdout}"
    )

    records = json.loads(record_path.read_text())
    by_label = {r["label"]: r["value"] for r in records}
    assert by_label.get("after_raising_test") == "entry-value"
    assert by_label.get("after_setup_failure") == "entry-value"


def test_leg2_control_confirms_the_hardcoded_teardown_shape_really_clobbers(
    tmp_path: Path,
) -> None:
    """Must-fire control, run as its own nested pytest: a `teardown_method`-shaped reset
    overwrites with a hardcoded default. Without this, leg 2 above could pass on a harness
    that isolates nothing at all.
    """
    tree = _write_leg2_tree(tmp_path / "leg2_control_run")
    record_path = tmp_path / "leg2_control_record.json"

    env = _child_env(
        PYTHONPATH=str(tree),
        LEG2_RECORD_PATH=str(record_path),
    )
    result = _run_pytest(
        ["test_leg2_control.py", "-p", "no:cacheprovider", "-q", "-rA"],
        cwd=tree,
        env=env,
    )
    assert "2 passed" in result.stdout, (
        f"control tree did not run as expected\n--- stdout ---\n{result.stdout}"
        f"\n--- stderr ---\n{result.stderr}"
    )
    records = json.loads(record_path.read_text())
    by_label = {r["label"]: r["value"] for r in records}
    assert by_label.get("control_after") == "hardcoded-default", (
        "must-fire control failed to fire -- the teardown_method shape did not clobber the "
        "value with its hardcoded default, so leg 2 would prove nothing"
    )
