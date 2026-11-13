"""A survivor must be reported WITH its file:line, where the operator reads it (#1373).

``mutation_survivor_lines.py`` worked on real data all along; the gap was that its output
went only to the end of the module log, past the summary block, and never to the console, so
a run showed bare ``<mutant>: survived`` ids. The fixture under
``tests/fixtures/mutmut_real_sample`` is a trimmed copy of mutmut 3.8.1's real output (its
``.spans`` index and generated mutants file), not a hand-made shape.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_LIB = _ROOT / "scripts" / "lib"
_SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "mutmut_real_sample"
_MP = "app/auth/lockout_store.py"
_MUTANT = "app.auth.lockout_store.x__get_redis_client__mutmut_13"


def _backend(tmp_path: Path) -> Path:
    """Lay the sample out as mutmut does: ``<b>/<path>``, ``<b>/mutants/<path>`` and ``.spans``.

    Stored with neutral extensions so the repo's duplicate-code guard does not read the
    fixture as a second copy of lockout_store.py.
    """
    be = tmp_path / "backend"
    (be / "app/auth").mkdir(parents=True)
    (be / "mutants/app/auth").mkdir(parents=True)
    shutil.copy(_SAMPLE / "source.txt", be / _MP)
    shutil.copy(_SAMPLE / "generated.txt", be / "mutants" / _MP)
    shutil.copy(_SAMPLE / "generated.spans.json", be / "mutants" / (_MP + ".spans"))
    return be


def test_the_locator_places_a_real_mutmut_survivor_on_its_source_line(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(_LIB))
    sys.modules.pop("mutation_survivor_lines", None)
    msl = importlib.import_module("mutation_survivor_lines")

    be = _backend(tmp_path)
    line, text = msl.locate(be, _MP, "x__get_redis_client__mutmut_13")

    assert text == "logger.warning(None, exc_info=True)"
    src = (be / _MP).read_text().splitlines()
    assert line is not None and "Redis connection failed for lockout storage" in src[line - 1]


def test_the_run_prints_the_locations_and_keeps_them_in_the_log(tmp_path):
    log = tmp_path / "lockout_store.log"
    shutil.copy(_SAMPLE / "summary.txt", log)
    script = f"""
        set -e
        RED= GREEN= YELLOW= NC=
        source {_LIB}/mutation-stack.sh
        SCRIPT_DIR={_ROOT}/scripts
        BACKEND={_backend(tmp_path)}
        declare -A MODULE_PATH=([lockout_store]=app/auth/lockout_store.py)
        report_survivor_locations lockout_store {log}
    """
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)

    assert r.returncode == 0, r.stderr
    row = next(ln for ln in r.stdout.splitlines() if ln.startswith(_MUTANT))
    assert row.split("\t")[1].startswith("app/auth/lockout_store.py:")
    assert row.split("\t")[1].rsplit(":", 1)[1].isdigit()
    assert row in log.read_text(), "the log must still carry the block after the END marker"
    assert "x__get_redis_client__mutmut_12" not in r.stdout, "killed mutants are not listed"


def _logged_run(tmp_path: Path) -> Path:
    """A module log as a finished run leaves it: summary block, then the locations block."""
    out = tmp_path / "out"
    out.mkdir()
    log = out / "lockout_store.log"
    shutil.copy(_SAMPLE / "summary.txt", log)
    script = f"""
        set -e
        source {_LIB}/mutation-stack.sh
        SCRIPT_DIR={_ROOT}/scripts
        BACKEND={_backend(tmp_path)}
        declare -A MODULE_PATH=([lockout_store]=app/auth/lockout_store.py)
        report_survivor_locations lockout_store {log} >/dev/null
    """
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return log


def test_the_parallel_parent_console_prints_each_modules_survivor_lines(tmp_path):
    """--parallel children write to <key>.console; the parent's console printed bare counts.

    Live run MUTRUN4 (2026-11-13): `lockout_store: mutants=111 killed=107 survived=4` and no
    file:line, although the child's log held the block. The parent now re-prints it per module.
    """
    log = _logged_run(tmp_path)
    rc_dir = tmp_path / "rc"
    rc_dir.mkdir()
    (rc_dir / "lockout_store").write_text("0\n")
    script = f"""
        RED= GREEN= YELLOW= BLUE= NC=
        source {_LIB}/mutation-stack.sh
        _mut_iso() {{ echo "judged $4"; }}
        REPO_ROOT={_ROOT} OUT_DIR={log.parent}
        mutation_parallel_report {rc_dir} lockout_store
    """
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)

    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    verdict = lines.index("  child exit 0, judge exit 0 (lockout_store)")
    row = next(i for i, ln in enumerate(lines) if ln.startswith(_MUTANT + "\t"))
    assert row > verdict, "the file:line follows the module's own verdict"
    assert lines[row].split("\t")[1].rsplit(":", 1)[1].isdigit()


def test_a_log_without_the_block_says_so_rather_than_printing_nothing(tmp_path):
    log = tmp_path / "lockout_store.log"
    shutil.copy(_SAMPLE / "summary.txt", log)
    script = f"source {_LIB}/mutation-stack.sh; logged_survivor_locations lockout_store {log}"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)

    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("lockout_store: no survivor file:line block in ")
    assert _MUTANT not in r.stdout


def test_a_serial_run_ends_with_its_survivor_lines(tmp_path):
    """The serial run printed the block ABOVE `mutmut results`, which lists every mutant of every
    module in backend/mutants/ (2,700 lines in MUTRUN4's lockout_store console). Now it is last."""
    log = _logged_run(tmp_path)
    script = f"""
        RED= GREEN= YELLOW= BLUE= NC=
        source {_LIB}/mutation-stack.sh
        OUT_DIR={log.parent}
        mutation_run_footer run-mutation-tests.sh lockout_store
    """
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)

    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[-1].startswith(_MUTANT + "\tapp/auth/lockout_store.py:"), lines[-3:]
    assert "Survivors of this run, by file:line" in lines
