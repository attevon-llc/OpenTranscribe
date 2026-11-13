"""Every file in MODULE_TESTS must exist.

A stale name (a renamed test file) makes mutmut's clean stats run fail with
BadTestExecutionCommandsException, so the module reports 0 mutants and never runs.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "run-mutation-tests.sh"


def _module_tests(text: str) -> dict[str, list[str]]:
    block = text[text.index("declare -A MODULE_TESTS") :]
    block = block[: block.index("\n)\n")]
    return {k: v.split() for k, v in re.findall(r'^\s*\[(\w+)\]="([^"]*)"', block, re.M)}


def test_every_module_tests_file_exists():
    mapping = _module_tests(_SCRIPT.read_text())
    assert len(mapping) >= 8, "parser found too few modules: the test is not looking at the table"
    missing = {
        k: [f for f in files if not (_ROOT / "backend" / f).is_file()]
        for k, files in mapping.items()
    }
    assert {k: v for k, v in missing.items() if v} == {}


def test_the_parser_flags_a_stale_name():
    text = 'declare -A MODULE_TESTS=(\n    [m]="tests/unit/test_gone.py"\n)\n'
    (mod_files,) = _module_tests(text).values()
    assert not (_ROOT / "backend" / mod_files[0]).is_file()
