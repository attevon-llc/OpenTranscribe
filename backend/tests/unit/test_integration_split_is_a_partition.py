"""The integration phase's two lanes together run exactly the unsplit selection.

``scripts/run-integration-tests.sh`` runs the throwaway-container integration modules
(``INTEGRATION_SELF_CONTAINED``) in a parallel xdist lane and the rest serially with one
``--ignore`` per listed module. A split is only a speed-up if it is lossless, so this collects all
three selections and checks, with the checker CI sharding uses, that serial + parallel is the
unsplit set, disjoint. It also pins the structural facts the partition depends on: every listed
path exists, the list has no duplicates, and the live lane's ignores are derived from the array.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]
_REPO = _BACKEND.parent
_GATE_SCRIPT = _REPO / "scripts" / "run-integration-tests.sh"
_SHARD_CHECK = _REPO / "scripts" / "guards" / "check_shard_coverage.py"
_LIVE_PATHS = ["tests/integration/", "tests/test_selective_reprocess.py", "tests/eval/"]

pytestmark = pytest.mark.unit


def self_contained_modules(source: str | None = None) -> list[str]:
    """Paths in the script's ``INTEGRATION_SELF_CONTAINED=( ... )`` array, backend-relative."""
    text = _GATE_SCRIPT.read_text(encoding="utf-8") if source is None else source
    match = re.search(r"^\s*INTEGRATION_SELF_CONTAINED=\(\n(.*?)\n\s*\)", text, re.M | re.S)
    assert match, "run-integration-tests.sh no longer defines INTEGRATION_SELF_CONTAINED"
    return re.findall(r"^\s*(tests/\S+\.py)\s*$", match.group(1), re.M)


def _collect(args: list[str]) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=", "-p",
         "no:cacheprovider", "-m", "integration", "--strict-markers", *args],
        cwd=_BACKEND, capture_output=True, text=True, check=False,
    )  # fmt: skip
    ids = [ln for ln in proc.stdout.splitlines() if "::" in ln]
    assert proc.returncode == 0 and ids, (proc.returncode, proc.stdout[-800:], proc.stderr[-800:])
    return ids


def test_the_array_is_nonempty_unique_and_every_path_exists() -> None:
    modules = self_contained_modules()
    assert modules, "the self-contained list is empty: the parallel lane would select nothing"
    assert len(modules) == len(set(modules)), "a module is listed twice"
    missing = [m for m in modules if not (_BACKEND / m).is_file()]
    assert not missing, (
        f"listed but absent (a rename silently shrinks the parallel lane): {missing}"
    )
    assert all(m.startswith("tests/integration/") for m in modules)


def test_the_live_lane_ignores_are_derived_from_the_array() -> None:
    source = _GATE_SCRIPT.read_text(encoding="utf-8")
    assert re.search(
        r'for _sc in "\$\{INTEGRATION_SELF_CONTAINED\[@\]\}"; do\s*\n'
        r'\s*INTEGRATION_LIVE_IGNORES\+=\("--ignore=\$_sc"\)',
        source,
    ), "the live lane's --ignore list is no longer built from INTEGRATION_SELF_CONTAINED"
    assert '"${INTEGRATION_LIVE_IGNORES[@]}"' in source, "the live lane does not apply the ignores"


def test_serial_plus_parallel_is_exactly_the_unsplit_selection() -> None:
    spec = importlib.util.spec_from_file_location("check_shard_coverage", _SHARD_CHECK)
    assert spec and spec.loader
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

    modules = self_contained_modules()
    ignores = [f"--ignore={m}" for m in modules]
    with ThreadPoolExecutor(max_workers=3) as pool:
        full_f = pool.submit(_collect, _LIVE_PATHS)
        live_f = pool.submit(_collect, [*_LIVE_PATHS, *ignores])
        self_f = pool.submit(_collect, modules)
        full, live, parallel = full_f.result(), live_f.result(), self_f.result()
    assert checker.check(full, {"live": live, "self-contained": parallel}) == []
    assert not set(live) & set(parallel)
