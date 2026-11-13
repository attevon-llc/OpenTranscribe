"""EFFB gate changes: G1b (no duplicate ids), T4 (background tail allow-list), SW1 (one entry sweep),
D4 (argv recording + failed-only replay), D2 (xdist worker hook), --affected (never evidence)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2]
_REPO = _BACKEND.parent
_GATE = (_REPO / "scripts" / "run-integration-tests.sh").read_text(encoding="utf-8")
_TAIL = (_REPO / "scripts" / "lib" / "gate-tail-phases.sh").read_text(encoding="utf-8")

#: Phases that read no live service, allowed to run beside the live lane (T4).
STACK_INDEPENDENT_PHASES = {
    "Dependency parity: venv vs container",
    "DB session lifetime (no transaction across slow work)",
    "Collection determinism (two processes, same test ids)",
    "Mutation ratchet (last run vs baselines)",
}


def _t4_body() -> str:
    m = re.search(r"^t4_stack_independent_phases\(\) \{\n(.*?)^\}\n", _TAIL, re.M | re.S)
    assert m, "t4_stack_independent_phases() is gone"
    return m.group(1)


def test_t4_titles_are_exactly_the_allow_list() -> None:
    titles = set(re.findall(r'run_phase\w* "([^"]+)"', _t4_body()))
    assert titles == STACK_INDEPENDENT_PHASES


def test_t4_commands_never_touch_the_stack() -> None:
    body = _t4_body()
    assert "docker" not in body
    # the only pytest in the tail is the two collect-only probes
    for line in body.splitlines():
        if "-m pytest" in line:
            assert "--collect-only" in line
    assert "REDIS_ENV" not in body and "INTEGRATION_ENV" not in body


def test_t4_titles_appear_once_each_and_are_joined() -> None:
    for t in STACK_INDEPENDENT_PHASES:
        assert (_GATE + _TAIL).count(f'"{t}"') == 1
    assert 'wait "$T4_PID"' in _GATE
    assert 'if [ -z "$T4_PID" ]; then\n    t4_stack_independent_phases' in _GATE


def test_g1b_gpu_phase_excludes_integration_marked_tests() -> None:
    assert 'GPU_SELECTION="${GPU_SELECTION:-gpu and not integration$MULTI_GPU_FILTER}"' in _GATE


def _collect(expr: str) -> set[str]:
    env = {**os.environ, "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "-1")}
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=", "-p", "no:cacheprovider",
         "-p", "no:testmon", "-m", expr, "--strict-markers", "tests/integration/",
         "tests/unit/test_cuda_device_guard.py"],
        cwd=_BACKEND, capture_output=True, text=True, check=False, env=env, timeout=600,
    )  # fmt: skip
    ids = {ln for ln in p.stdout.splitlines() if "::" in ln}
    assert ids, (p.returncode, p.stdout[-500:], p.stderr[-500:])
    return ids


def test_g1b_integration_and_gpu_phases_are_disjoint_with_same_union() -> None:
    integ = _collect("integration")
    old_gpu = _collect("gpu")
    new_gpu = _collect("gpu and not integration")
    assert integ & new_gpu == set()
    assert integ | new_gpu == integ | old_gpu
    assert old_gpu & integ, "no id was duplicated before: G1b has nothing to prove"


def test_sw1_nested_entry_sweep_runs_once(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    counter = tmp_path / "calls"
    (root / "scripts" / "cleanup-test-data.py").write_text(
        f"import pathlib\npathlib.Path({str(counter)!r}).open('a').write('x')\n"
    )
    lib = _REPO / "scripts" / "lib" / "test-data-sweep.sh"
    script = (
        f'source "{lib}"; TDS_PYTHON="{sys.executable}"; TDS_ROOT="{root}"; '
        "tds_own_entry_sweep_for_children; "
        # a child: the integration script's own branch, mirrored below, skips on the token
        'if [ "${OT_TEST_DATA_SWEEP_AT_ENTRY:-}" = parent ]; then :; else tds_run_sweep start; fi'
    )
    subprocess.run(["bash", "-c", script], check=True, timeout=60, capture_output=True)
    assert counter.read_text() == "x"
    # the child branch in the real script is exactly that token test, under the same title
    assert 'elif [ "${OT_TEST_DATA_SWEEP_AT_ENTRY:-}" = "parent" ]' in _GATE
    assert 'run_phase "Test-data sweep (Tier A)"' in _GATE
    assert "tds_own_entry_sweep_for_children" in (_REPO / "scripts/run-dev-tests.sh").read_text()


def test_d4_junit_failed_ids_and_replay_refusal(tmp_path: Path) -> None:
    xml = tmp_path / "unit.xml"
    xml.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="tests.unit.test_gate_effb_phase_changes" name="test_a"><failure/></testcase>'
        '<testcase classname="tests.unit.test_gate_effb_phase_changes" name="test_b"/>'
        '<testcase classname="tests.unit.test_gate_effb_phase_changes" name="test_c[x]"><error/></testcase>'
        "</testsuite></testsuites>"
    )
    out = subprocess.run(
        [sys.executable, str(_REPO / "scripts/lib/junit_failed.py"), str(xml), str(_BACKEND)],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout.split()  # fmt: skip
    p = "tests/unit/test_gate_effb_phase_changes.py"
    assert out == [f"{p}::test_a", f"{p}::test_c[x]"]
    # no .argv beside it: refuses
    r = subprocess.run(["bash", str(_REPO / "scripts/rerun-failed.sh"), str(tmp_path)],
                       capture_output=True, text=True, timeout=60, check=False)  # fmt: skip
    assert r.returncode == 2 and "REFUSED" in r.stderr and "NOT EVIDENCE" in r.stdout


def test_d4_replay_substitutes_ids_and_env(tmp_path: Path) -> None:
    (tmp_path / "unit.xml").write_text(
        '<testsuite><testcase classname="tests.unit.test_x" name="test_a"><failure/></testcase></testsuite>'
    )
    stub = tmp_path / "stub.sh"
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$@" >"$OUT"; echo "TOKEN=$MY_TOKEN" >>"$OUT"\n')
    stub.chmod(0o755)
    (tmp_path / "unit.xml.argv").write_text(
        f"env\nMY_TOKEN=@ENV\n{stub}\ntests/\n-q\n--junitxml=/old/unit.xml\n"
    )
    rec = tmp_path / "rec"
    env = {**os.environ, "MY_TOKEN": "s3", "OUT": str(rec)}
    r = subprocess.run(["bash", str(_REPO / "scripts/rerun-failed.sh"), str(tmp_path)],
                       capture_output=True, text=True, env=env, timeout=60, check=False)  # fmt: skip
    assert r.returncode == 0, r.stderr
    got = rec.read_text()
    assert "tests::unit::test_x::test_a" in got  # no such file on disk: unresolved fallback id
    assert "--junitxml=" + str(tmp_path / "unit.xml.rerun.xml") in got
    assert "TOKEN=s3" in got and "tests/\n" not in got


def test_gate_records_argv_without_secret_values() -> None:
    assert "record_phase_argv" in _GATE and "=@ENV" in _TAIL


def _hook():
    spec_path = _BACKEND / "tests" / "conftest.py"
    text = spec_path.read_text()
    m = re.search(r"^def pytest_xdist_auto_num_workers.*?(?=^def |\Z)", text, re.M | re.S)
    assert m
    ns: dict = {"os": os, "pytest": SimpleNamespace(Config=object)}
    exec("from __future__ import annotations\n" + m.group(0), ns)  # noqa: S102 - test-only extraction of one function
    return ns["pytest_xdist_auto_num_workers"]


@pytest.mark.parametrize(
    ("args", "env", "want"),
    [
        (["tests/unit/a.py"], {}, 1),
        (["tests/unit/a.py::t", "tests/unit/b.py"], {}, 2),
        (["tests/unit"], {}, None),
        (["tests/unit/a.py"], {"PYTEST_XDIST_AUTO_NUM_WORKERS": "7"}, None),
        ([f"tests/unit/f{i}.py" for i in range(5)], {}, None),
        ([], {}, None),
    ],
)
def test_d2_hook(args, env, want, monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert _hook()(SimpleNamespace(args=args)) == want


def test_affected_refuses_coverage_and_never_publishes() -> None:
    r = subprocess.run(["bash", str(_REPO / "scripts/run-backend-tests.sh"), "--affected", "--cov=app"],
                       capture_output=True, text=True, timeout=60, check=False)  # fmt: skip
    assert r.returncode == 2
    src = (_REPO / "scripts/run-backend-tests.sh").read_text()
    assert src.index('if $AFFECTED; then\n    echo -e "${YELLOW}$AFFECTED_BANNER') < src.index(
        'publish_last "$RUN_LOG" "$LOG"'
    )
