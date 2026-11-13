"""The release path never selects tests by change or by previous outcome.

pytest-testmon (``run-backend-tests.sh --affected``), ``--lf``/``--sw`` and the failed-only replay
helper are dev-loop aids. A gate that used one would report a green run over a subset. The ONE file
that names the flags is ``scripts/lib/no-selection.sh`` (it refuses them and defines NO_SELECT).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "scripts" / "lib" / "no-selection.sh"
FORBIDDEN = re.compile(
    r"(?<![\w-])(--testmon-forceselect|--testmon|--last-failed|--failed-first|--stepwise|--picked"
    r"|--lf|--ff|--sw|--affected|rerun-failed)(?![\w-])"
)
GATE_SCRIPTS = (
    "scripts/run-integration-tests.sh",
    "scripts/run-dev-tests.sh",
    "scripts/test-matrix.sh",
)
GATE_GLOBS = ("scripts/release/*.sh", "scripts/release-tests/**/*.sh", "scripts/e2e/*.sh")


def find_tokens(text: str) -> list[str]:
    """Every selection token in ``text`` (comment lines included, on purpose)."""
    return FORBIDDEN.findall(text)


def _gate_files() -> list[Path]:
    files = [REPO / p for p in GATE_SCRIPTS]
    for g in GATE_GLOBS:
        files += sorted(REPO.glob(g))
    return [f for f in files if f.is_file()]


def test_gate_file_set_is_not_empty() -> None:
    names = {f.name for f in _gate_files()}
    assert {"run-integration-tests.sh", "run-dev-tests.sh", "60-test.sh"} <= names


@pytest.mark.parametrize("path", _gate_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_release_path_script_names_no_selection_flag(path: Path) -> None:
    assert find_tokens(path.read_text()) == []


@pytest.mark.parametrize(
    "tok",
    [
        "--testmon",
        "--testmon-forceselect",
        "--lf",
        "--last-failed",
        "--ff",
        "--failed-first",
        "--sw",
        "--stepwise",
        "--picked",
        "--affected",
        "rerun-failed",
    ],
)
def test_detector_fires_on_every_token(tok: str) -> None:
    assert find_tokens(f"# example: pytest {tok} tests/") == [tok]


def test_detector_stays_clean_on_lookalikes() -> None:
    assert find_tokens("--swap --lfs --ffmpeg --software --no-testmon --lf-ish") == []


def test_every_gate_pytest_call_disables_selection_plugins() -> None:
    text = (REPO / "scripts/run-integration-tests.sh").read_text()
    calls = [
        ln for ln in text.splitlines() if "-m pytest" in ln and not ln.lstrip().startswith("#")
    ]
    assert len(calls) >= 9
    bad = [ln for ln in calls if "NO_SELECT" not in ln and "no:testmon" not in ln]
    assert bad == []


def _run(script: str, env_extra: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, **env_extra}
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}"; refuse_selection_flags "$@"; echo PASSED', "x", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("opt", ["--testmon", "--lf", "-x --sw", "--picked", "--ff"])
def test_refusal_via_pytest_addopts(opt: str) -> None:
    r = _run("", {"PYTEST_ADDOPTS": opt})
    assert r.returncode == 2 and "PASSED" not in r.stdout


def test_refusal_via_argument() -> None:
    assert _run("", {"PYTEST_ADDOPTS": ""}, "--backend-only", "--stepwise").returncode == 2


def test_clean_environment_passes_and_defines_no_select() -> None:
    r = _run("", {"PYTEST_ADDOPTS": "-x"}, "--backend-only")
    assert r.returncode == 0 and "PASSED" in r.stdout
    out = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; echo "${{NO_SELECT[*]}}"'],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    assert out == "-p no:testmon -p no:stepwise"


@pytest.mark.parametrize("script", ["run-integration-tests.sh", "run-dev-tests.sh"])
def test_entry_scripts_refuse_before_doing_anything(script: str) -> None:
    import os

    r = subprocess.run(
        ["bash", str(REPO / "scripts" / script), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTEST_ADDOPTS": "--testmon"},
    )
    assert r.returncode == 2
    assert "REFUSED" in r.stderr
