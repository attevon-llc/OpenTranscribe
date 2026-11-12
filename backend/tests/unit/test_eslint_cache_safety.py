"""Guards for the ESLint result cache: it is valid for the base config only (ESLint docs).

A cached result is replayed for any file whose content and config are unchanged, which is exact
only when the rules read nothing but that file. Type-aware rules read other files, so they must
never run with ``--cache``, and the base config must never grow one.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
_FRONTEND = _REPO / "frontend"


def _scripts() -> dict[str, str]:
    scripts: dict[str, str] = json.loads((_FRONTEND / "package.json").read_text(encoding="utf-8"))[
        "scripts"
    ]
    return scripts


def test_type_aware_lint_script_never_uses_the_cache() -> None:
    assert "--cache" not in _scripts()["lint:types"]


def test_eslint_typed_hook_never_uses_the_cache() -> None:
    config = yaml.safe_load((_REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = {h["id"]: h for repo in config["repos"] for h in repo["hooks"]}
    assert "--cache" not in hooks["eslint-typed"]["entry"]


def test_base_lint_script_is_content_keyed() -> None:
    lint = _scripts()["lint"]
    assert "--cache-strategy content" in lint
    assert ".cache/eslint/" in lint  # an ignored path, never node_modules


def test_base_config_has_no_type_aware_or_import_resolving_rules() -> None:
    text = (_FRONTEND / "eslint.config.js").read_text(encoding="utf-8")
    assert not re.search(r"projectService|project\s*:|import/|eslint-plugin-import", text)


@pytest.mark.skipif(
    not (_FRONTEND / "node_modules" / ".bin" / "eslint").exists(),
    reason="frontend deps not installed",
)
def test_cached_run_still_reports_an_edit_that_introduces_a_violation(tmp_path: Path) -> None:
    """Negative control: warm the cache on a clean file, then break it, and expect the error."""
    (tmp_path / "eslint.config.js").write_text(
        "export default [{ files: ['**/*.js'], rules: { 'prefer-const': 'error' } }];\n",
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    target = tmp_path / "a.js"

    def lint() -> int:
        return subprocess.run(
            [
                str(_FRONTEND / "node_modules" / ".bin" / "eslint"),
                "a.js",
                "--cache",
                "--cache-strategy",
                "content",
                "--cache-location",
                ".cache/eslint/t/",
            ],
            cwd=tmp_path,
            capture_output=True,
            check=False,
            timeout=300,
        ).returncode

    target.write_text("const a = 1;\nexport default a;\n", encoding="utf-8")
    assert lint() == 0
    assert lint() == 0  # replayed from the cache
    target.write_text("let a = 1;\nexport default a;\n", encoding="utf-8")
    assert lint() == 1
