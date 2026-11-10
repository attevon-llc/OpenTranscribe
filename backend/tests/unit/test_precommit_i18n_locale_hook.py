"""A commit that touches only locale JSON must still run the i18n key-parity check.

``frontend-check`` runs ``npm run check:i18n``, but its filter is ``files: ^frontend/src/`` plus
``types_or: [svelte, ts, javascript, css, html]``. ``identify`` tags a locale file ``json``, and
pre-commit runs a hook only when a file matches ``files`` AND its tags satisfy ``types`` /
``types_or`` (https://pre-commit.com/#filtering-files-with-types). So a commit editing only
``frontend/src/lib/i18n/locales/*.json`` selected no hook, and a parity break introduced purely in
translation files looked clean locally and failed only in CI.

The ``i18n-parity`` hook closes that gap. These tests read the real config and apply pre-commit's
own selection rule with the same ``identify`` library pre-commit uses, then run the hook's entry
against a copy of the locales with one key removed to prove it goes red.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from identify import identify

REPO_ROOT = Path(__file__).resolve().parents[3]
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
LOCALES = REPO_ROOT / "frontend" / "src" / "lib" / "i18n" / "locales"
PARITY_SCRIPT = REPO_ROOT / "frontend" / "scripts" / "check-i18n-parity.mjs"


def _hook(hook_id: str) -> dict[str, Any]:
    config = yaml.safe_load(PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == hook_id:
                return dict(hook)
    raise AssertionError(f"hook {hook_id!r} is not in {PRECOMMIT_CONFIG}")


def _selects(hook: dict[str, Any], rel_path: str) -> bool:
    """pre-commit's file selection: ``files`` regex, ``exclude``, then ``types`` and ``types_or``."""
    if not re.search(hook.get("files", ""), rel_path):
        return False
    if hook.get("exclude") and re.search(hook["exclude"], rel_path):
        return False
    tags = identify.tags_from_path(str(REPO_ROOT / rel_path))
    if not set(hook.get("types", ["file"])) <= tags:
        return False
    types_or = hook.get("types_or", [])
    return not types_or or bool(set(types_or) & tags)


def _locale_paths() -> list[str]:
    paths = sorted(p.relative_to(REPO_ROOT).as_posix() for p in LOCALES.glob("*.json"))
    assert len(paths) == 12, f"expected the 12 shipped locales, found {paths}"
    return paths


def test_frontend_check_does_not_select_locale_json() -> None:
    # The control: the gap this hook exists for. If frontend-check ever starts selecting
    # locale files, the i18n-parity hook is redundant and should be removed.
    hook = _hook("frontend-check")
    assert not any(_selects(hook, p) for p in _locale_paths())


def test_i18n_parity_hook_selects_every_locale_file_at_commit_stage() -> None:
    hook = _hook("i18n-parity")
    assert hook.get("stages") == ["pre-commit"]
    assert hook.get("pass_filenames") is False
    unselected = [p for p in _locale_paths() if not _selects(hook, p)]
    assert unselected == []


def test_i18n_parity_hook_ignores_unrelated_json() -> None:
    hook = _hook("i18n-parity")
    assert not _selects(hook, "frontend/package.json")
    assert not _selects(hook, "frontend/tsconfig.json")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_i18n_parity_entry_fails_on_a_missing_key_and_passes_on_the_real_tree(
    tmp_path: Path,
) -> None:
    entry = shlex.split(_hook("i18n-parity")["entry"])
    assert entry[0] == "node"
    assert (REPO_ROOT / entry[1]) == PARITY_SCRIPT

    # The script resolves the locales relative to itself, so mirror the two directories.
    script = tmp_path / "frontend" / "scripts" / PARITY_SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy(PARITY_SCRIPT, script)
    locales = tmp_path / "frontend" / "src" / "lib" / "i18n" / "locales"
    shutil.copytree(LOCALES, locales)

    green = subprocess.run([entry[0], str(script)], capture_output=True, text=True, check=False)
    assert green.returncode == 0, green.stdout + green.stderr

    de = locales / "de.json"
    text = de.read_text(encoding="utf-8")
    # Drop the first leaf key/value line; the file stays valid JSON only if it was not the
    # last member of its object, so pick a line that ends with a comma.
    lines = text.splitlines(keepends=True)
    victim = next(i for i, line in enumerate(lines) if re.match(r'\s*"[^"]+":\s*".*",\s*$', line))
    del lines[victim]
    de.write_text("".join(lines), encoding="utf-8")

    red = subprocess.run([entry[0], str(script)], capture_output=True, text=True, check=False)
    assert red.returncode != 0
    assert "de.json" in red.stdout + red.stderr
