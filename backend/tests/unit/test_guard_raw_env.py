"""Tests for ``scripts/guards/check_raw_env.py`` (raw environment reads in backend/app)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "guards" / "check_raw_env.py"
_REAL_ROOT = _SCRIPT.parents[2]
REASON = "env: single reader planned"


def _load():
    spec = importlib.util.spec_from_file_location("_check_raw_env_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / "backend" / "app" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _run(root: Path, *extra: str) -> int:
    baseline = root / "baseline.txt"
    code: int = guard.main(["--root", str(root), "--baseline", str(baseline), *extra])
    return code


def _write_baseline(root: Path) -> None:
    assert _run(root, "--write-baseline", "--reason", REASON) == 0


PROBE = "services/_probe.py"


def test_planted_getenv_fails_against_empty_baseline(tmp_path, capsys):
    root = _repo(tmp_path, {PROBE: "import os\nX = os.getenv('A')\n"})
    (root / "baseline.txt").write_text("")
    assert _run(root) == 1
    assert "backend/app/services/_probe.py" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import os\nos.getenv('A')\n", 1),
        ("import os\nos.environ['A']\n", 1),
        ("import os\nos.environ.get('A')\nos.environ.setdefault('B', '1')\n", 2),
        ("import os as _os\n_os.getenv('A')\n", 1),
        ("from os import getenv\ngetenv('A')\n", 1),
        ("from os import environ as e\ne.get('A')\n", 1),
        ("def f():\n    import os\n    return os.getenv('A')\n", 1),
        ("import os\nclass C:\n    def m(self):\n        return os.environ.get('A')\n", 1),
    ],
)
def test_aliases_and_nested_bodies_are_counted(source, expected):
    import ast

    assert guard.count_sites(ast.parse(source)) == expected


def test_comment_and_string_mentions_are_ignored(tmp_path):
    src = '# os.getenv("A")\n"""os.environ["A"]"""\nS = "os.getenv(1)"\n'
    root = _repo(tmp_path, {PROBE: src})
    assert guard.scan(root) == {}


def test_unrelated_object_named_environ_is_ignored():
    import ast

    assert (
        guard.count_sites(ast.parse("import os\nconfig.environ.get('A')\nos.path.join('a')\n")) == 0
    )


def test_core_config_is_exempt(tmp_path):
    root = _repo(tmp_path, {"core/config.py": "import os\nos.getenv('A')\n", PROBE: "x = 1\n"})
    assert guard.scan(root) == {}


def test_grown_count_fails_and_matching_baseline_passes(tmp_path):
    root = _repo(tmp_path, {PROBE: "import os\nos.getenv('A')\n"})
    _write_baseline(root)
    assert _run(root) == 0
    (root / "backend/app/services/_probe.py").write_text(
        "import os\nos.getenv('A')\nos.getenv('B')\n"
    )
    assert _run(root) == 1


def test_loosened_and_stale_entries_fail_and_tighten_repairs(tmp_path):
    root = _repo(
        tmp_path,
        {
            PROBE: "import os\nos.getenv('A')\nos.getenv('B')\n",
            "a.py": "import os\nos.getenv('A')\n",
        },
    )
    _write_baseline(root)
    _repo(root, {PROBE: "import os\nos.getenv('A')\n", "a.py": "x = 1\n"})
    assert _run(root) == 1
    assert _run(root, "--tighten") == 0
    assert _run(root) == 0
    text = (root / "baseline.txt").read_text()
    assert "a.py" not in text
    assert f"backend/app/services/_probe.py\t1\t{REASON}" in text


def test_syntax_error_fails_closed(tmp_path, capsys):
    root = _repo(tmp_path, {PROBE: "def broken(:\n"})
    (root / "baseline.txt").write_text("")
    assert _run(root) == 2
    assert "cannot parse" in capsys.readouterr().err


def test_missing_app_dir_fails_closed(tmp_path):
    assert _run(tmp_path) == 2


def test_write_baseline_requires_real_reason(tmp_path):
    root = _repo(tmp_path, {PROBE: "import os\nos.getenv('A')\n"})
    assert _run(root, "--write-baseline", "--reason", "TODO") == 2


def test_real_repo_passes_with_committed_baseline():
    assert os.path.isdir(_REAL_ROOT / "backend" / "app")
    assert guard.main(["--root", str(_REAL_ROOT)]) == 0
