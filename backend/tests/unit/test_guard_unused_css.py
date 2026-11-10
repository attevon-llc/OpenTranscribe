"""Tests for ``scripts/guards/check_unused_css.py`` (global CSS classes nothing references)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "guards" / "check_unused_css.py"
_REAL_ROOT = _SCRIPT.parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("_check_unused_css_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A git repo holding ``files`` (the guard reads ``git ls-files``, so they are added)."""
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def _run(root: Path, *extra: str, allow: str = "") -> int:
    allowlist = root / "allow.txt"
    allowlist.write_text(allow, encoding="utf-8")
    code: int = guard.main(
        [
            "--root",
            str(root),
            "--baseline",
            str(root / "baseline.txt"),
            "--allowlist",
            str(allowlist),
            *extra,
        ]
    )
    return code


CSS = "frontend/src/styles/app.css"
COMP = "frontend/src/components/Card.svelte"


def test_unreferenced_global_class_fails(tmp_path, capsys):
    root = _repo(tmp_path, {CSS: ".dead { color: red; }\n", COMP: "<div></div>\n"})
    assert _run(root) == 1
    assert "frontend/src/styles/app.css::dead" in capsys.readouterr().err


@pytest.mark.parametrize(
    "markup",
    [
        '<div class="card live"></div>',
        "<div class:live={on}></div>",
        "<script>el.classList.add('live');</script>",
        "<script>const c = clsx({ live: on });</script>",
    ],
)
def test_static_and_directive_references_count(tmp_path, markup):
    root = _repo(tmp_path, {CSS: ".live { color: red; }\n", COMP: markup + "\n"})
    assert _run(root) == 0


def test_a_style_block_is_a_definition_not_a_use(tmp_path):
    comp = "<div></div>\n<style>\n  :global(.only-styled) { color: red; }\n</style>\n"
    root = _repo(tmp_path, {CSS: ".only-styled { margin: 0; }\n", COMP: comp})
    assert _run(root) == 1


def test_component_global_selector_is_checked_and_scoped_one_is_not(tmp_path, capsys):
    comp = (
        '<div class="wrap"></div>\n<style>\n  .wrap :global(.ghost) { color: red; }\n'
        "  .scoped-only { color: blue; }\n</style>\n"
    )
    root = _repo(tmp_path, {COMP: comp})
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "Card.svelte::ghost" in err
    assert "scoped-only" not in err
    assert "::wrap" not in err


def test_dynamic_prefix_marks_its_family_referenced(tmp_path):
    comp = "<script>const cls = `badge-${tone}`;</script>\n<span class={cls}></span>\n"
    root = _repo(tmp_path, {CSS: ".badge-ok { color: green; }\n", COMP: comp})
    assert _run(root) == 0


def test_a_reference_from_a_test_file_does_not_count(tmp_path):
    root = _repo(
        tmp_path,
        {
            CSS: ".tested-only { color: red; }\n",
            "frontend/src/components/Card.test.ts": "q('.tested-only');\n",
        },
    )
    assert _run(root) == 1


def test_values_and_at_rule_preludes_are_not_classes(tmp_path):
    css = "@media (min-width: 1.5rem) { .x { margin: 0.5rem; background: url(a.b.png); } }\n"
    root = _repo(tmp_path, {CSS: css, COMP: '<i class="x"></i>\n'})
    assert _run(root) == 0


def test_allowlist_pattern_exempts_and_a_stale_entry_fails(tmp_path, capsys):
    root = _repo(tmp_path, {CSS: ".hljs-keyword { color: red; }\n", COMP: "<i></i>\n"})
    good = "hljs-*\t1\tthird-party-dom: highlight.js renders these spans\n"
    assert _run(root, allow=good) == 0
    stale = good + "gone\t1\tthird-party-dom: a library class nothing defines any more\n"
    assert _run(root, allow=stale) == 1
    assert "gone: matches no defined class" in capsys.readouterr().err


def test_allowlist_reason_must_carry_a_class(tmp_path):
    root = _repo(tmp_path, {CSS: ".hljs-keyword { color: red; }\n", COMP: "<i></i>\n"})
    assert _run(root, allow="hljs-*\t1\tbecause the library renders it\n") == 2


def test_unbalanced_braces_fail_closed(tmp_path):
    root = _repo(tmp_path, {CSS: ".a { color: red;\n", COMP: '<i class="a"></i>\n'})
    assert _run(root) == 2


def test_baseline_freezes_existing_debt_and_goes_stale_when_fixed(tmp_path):
    root = _repo(tmp_path, {CSS: ".dead { color: red; }\n", COMP: "<i></i>\n"})
    assert _run(root, "--write-baseline", "--reason", "css: legacy class burn-down") == 0
    assert _run(root) == 0
    (root / COMP).write_text('<i class="dead"></i>\n', encoding="utf-8")
    assert _run(root) == 1


def test_real_tree_is_clean():
    assert guard.main(["--root", str(_REAL_ROOT)]) == 0
