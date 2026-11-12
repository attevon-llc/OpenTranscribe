"""Tests for ``scripts/guards/check_style_literals.py`` (frontend styling-layer ratchet).

A guard that matches nothing reports a clean tree, so every category has a must-fire fixture and
a must-stay-clean fixture, and the baseline mechanics (new / grown / shrunk / stale / unreadable)
are exercised end to end against a throwaway git repository.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "guards" / "check_style_literals.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_style_literals_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()
COMPONENT = "frontend/src/components/X.svelte"


def counts(css: str, rel: str = "frontend/src/components/x.css") -> dict[str, int]:
    return dict(guard.count_source(rel, css))


def svelte(style: str = "", markup: str = "", script: str = "") -> dict[str, int]:
    source = f"<script>{script}</script>\n{markup}\n<style>{style}</style>\n"
    return dict(guard.count_source(COMPONENT, source))


# --- must fire: one fixture per category --------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    ("category", "css", "expected"),
    [
        ("colour", ".a { color: #fff; }", 1),
        ("colour", ".a { color: #1e293b; background: #fff; }", 2),
        ("colour", ".a { background: rgba(0, 0, 0, 0.5); }", 1),
        (
            "colour",
            ".a { border: 1px solid hsl(210 40% 90%); }",
            1,
        ),  # a numeric first channel counts in any syntax
        ("colour", ".a { color: hsl(210, 40%, 90%); }", 1),
        ("colour", ".a { color: white; }", 1),
        ("colour", ".a { --brand: #123456; }", 1),
        ("fallback", ".a { color: var(--text-color, #333); }", 1),
        ("fallback", ".a { margin: var(--space-2, 8px) var(--space-3, 12px); }", 2),
        ("shadow", ".a { box-shadow: 0 1px 3px rgba(0,0,0,.1); }", 1),
        ("radius", ".a { border-radius: 8px; }", 1),
        ("radius", ".a { border-radius: 4px 4px 0 0; }", 2),
        ("radius", ".a { border-top-left-radius: 50%; }", 1),
        ("spacing", ".a { padding: 0.5rem 1rem; }", 2),
        ("spacing", ".a { margin-inline-start: 12px; }", 1),
        ("spacing", ".a { gap: 8px; }", 1),
        ("font-size", ".a { font-size: 0.875rem; }", 1),
        ("font-weight", ".a { font-weight: 600; }", 1),
        ("font-weight", ".a { font-weight: bold; }", 1),
        ("z-index", ".a { z-index: 1300; }", 1),
        ("duration", ".a { transition: color 0.2s ease; }", 1),
        ("duration", ".a { transition: opacity 0.2s ease 0.1s; }", 2),
        ("physical", ".a { margin-left: 0; }", 1),
        ("physical", ".a { border-right: 1px solid red; }", 1),
        ("physical", ".a { left: 0; }", 1),
        ("physical", ".a { text-align: right; }", 1),
        ("physical", ".a { float: left; }", 1),
        ("alias", ".a { color: var(--text-muted); }", 1),
        ("alias", ".a { border: 1px solid var(--color-border); background: var(--hover-bg); }", 2),
    ],
)
def test_category_fires(category: str, css: str, expected: int) -> None:
    assert counts(css).get(category, 0) == expected


# --- must stay clean ----------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    ("category", "css"),
    [
        ("colour", ".a { background: rgba(var(--primary-color-rgb), 0.2); }"),
        ("colour", ".a { color: transparent; border-color: currentColor; background: none; }"),
        ("colour", ".a { color: var(--text-color); }"),
        ("colour", ".a { color: var(--white); }"),  # a token NAME is not the colour white
        ("colour", ".a { /* color: #fff; */ color: var(--text-color); }"),
        ("colour", ".a { background: url('data:image/svg+xml;utf8,<svg fill=\"#fff\"/>'); }"),
        ("colour", '.a { content: "#fff"; }'),
        ("colour", ".a { font-family: Tahoma, sans-serif; animation: orange 1s; }"),
        ("fallback", ".a { color: var(--text-color); }"),
        ("shadow", ".a { box-shadow: none; }"),
        ("shadow", ".a { box-shadow: var(--shadow-sm); }"),
        ("shadow", ".a { box-shadow: var(--shadow-sm), var(--shadow-lg); }"),
        ("radius", ".a { border-radius: 0; }"),
        ("radius", ".a { border-radius: var(--radius-lg); }"),
        ("radius", ".a { border-radius: calc(var(--radius-lg) - 2px); }"),
        ("spacing", ".a { padding: 0 1px; margin: -1px auto; }"),
        ("spacing", ".a { padding: var(--space-2) var(--space-3); gap: var(--space-2); }"),
        ("spacing", ".a { margin: 0 auto; padding: 50% 0; padding-block: 1em; }"),
        ("spacing", ".a { padding: calc(var(--space-2) + 2px); }"),
        ("font-size", ".a { font-size: var(--font-size-md); }"),
        ("font-size", ".a { font-size: inherit; }"),
        ("font-weight", ".a { font-weight: var(--font-weight-medium); }"),
        ("z-index", ".a { z-index: var(--z-modal); }"),
        ("z-index", ".a { z-index: 0; } .b { z-index: -1; } .c { z-index: auto; }"),
        ("duration", ".a { transition: color var(--duration-base) ease; }"),
        ("duration", ".a { animation: spin 0.8s linear infinite; }"),  # loops are not counted
        ("duration", ".a { transition: none; }"),
        ("physical", ".a { margin-inline-start: 0; padding-inline: 0; text-align: start; }"),
        ("physical", ".a { left: 50%; transform: translateX(-50%); }"),
        ("physical", ".a { border-top-left-radius: var(--radius-sm); }"),
        ("alias", ".a { color: var(--text-secondary); border-color: var(--border-color); }"),
        ("alias", ".a { background: var(--card-background); }"),  # not a pure alias: kept
    ],
)
def test_category_stays_clean(category: str, css: str) -> None:
    assert counts(css).get(category, 0) == 0


@pytest.mark.unit
def test_a_literal_inside_a_fallback_is_counted_once_by_fallback() -> None:
    """``var(--x, 8px)`` is a fallback, not also a spacing literal."""
    found = counts(".a { padding: var(--space-2, 8px); }")
    assert found == {"fallback": 1}


# --- constructs (R12) ---------------------------------------------------------------------------
@pytest.mark.unit
def test_nested_at_rules_and_global_selectors() -> None:
    css = """
    @media (min-width: 600px) {
      :global([data-theme='dark']) .a:hover { color: #fff; padding: 8px; }
    }
    @supports (display: grid) { .b { border-radius: 4px; } }
    @keyframes k { 0% { opacity: 0 } 100% { opacity: 1; margin-left: 4px } }
    """
    found = counts(css)
    assert found["colour"] == 1
    assert found["spacing"] == 2  # padding: 8px and the keyframe's margin-left: 4px
    assert found["radius"] == 1
    assert found["physical"] == 1


@pytest.mark.unit
def test_last_declaration_without_semicolon_is_seen() -> None:
    assert counts(".a { color: #fff }")["colour"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "css", [".a { color: #fff;", ".a { color: #fff; } }", ".a { color: rgb(1, 2, 3 }"]
)
def test_unbalanced_input_is_an_error_not_a_clean_pass(css: str) -> None:
    with pytest.raises(guard.bl.ScanError):
        guard.count_source("frontend/src/x.css", css)


# --- svelte -------------------------------------------------------------------------------------
@pytest.mark.unit
def test_style_attribute_and_directive_literals_count() -> None:
    found = svelte(markup='<div style="color: #fff; padding: 8px" style:z-index="40"></div>')
    assert found["colour"] == 1
    assert found["spacing"] == 1
    assert found["z-index"] == 1


@pytest.mark.unit
def test_template_expressions_and_script_strings_are_not_literals() -> None:
    found = svelte(
        markup='<div style="width: {pct}%; color: {c}"></div>',
        script="const s = '<div style=\"color: #fff\">';",
    )
    assert found == {}


@pytest.mark.unit
def test_style_block_literals_count_and_html_comments_do_not() -> None:
    found = svelte(style=".a { color: #fff; }", markup="<!-- <p style='color:#000'> -->")
    assert found == {"colour": 1}


# --- exemptions ---------------------------------------------------------------------------------
@pytest.mark.unit
def test_theme_css_is_out_of_scope_and_tests_are_too() -> None:
    assert not guard._in_scope("frontend/src/styles/theme.css")
    assert not guard._in_scope("frontend/src/components/x.test.ts")
    assert not guard._in_scope("frontend/src/components/Widget.test.svelte")
    assert guard._in_scope("frontend/src/styles/form-elements.css")
    assert guard._in_scope(COMPONENT)


@pytest.mark.unit
def test_component_token_sources_are_exempt_but_only_in_styles_root_blocks() -> None:
    root_block = ":root { --field-radius: 8px; --field-border: #d1d5db; }"
    assert counts(root_block, "frontend/src/styles/form-elements.css") == {}
    # The same declaration in a component, or in a non-root block, is a literal.
    assert counts(root_block, "frontend/src/components/x.css").get("colour") == 1
    other = ".a { --field-border: #d1d5db; }"
    assert counts(other, "frontend/src/styles/form-elements.css").get("colour") == 1


# --- baseline mechanics, end to end -------------------------------------------------------------
def _repo(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def _run(root: Path, *extra: str) -> int:
    return int(guard.main(["--root", str(root), "--baseline", str(root / "base.txt"), *extra]))


REASON = "styling layer: pre-migration literal, burn-down planned in the styling plan"


@pytest.mark.unit
def test_baseline_ratchet_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    css = "frontend/src/components/a.css"
    root = _repo(tmp_path, {css: ".a { color: #fff; background: #000; }\n"})

    assert _run(root) == 1  # no baseline yet: both literals are new debt
    assert _run(root, "--write-baseline", "--reason", REASON) == 0
    assert _run(root) == 0

    (root / css).write_text(".a { color: #fff; background: #000; border-color: #111; }\n")
    assert _run(root) == 1  # grew past the baseline
    assert "grew past" in capsys.readouterr().err

    (root / css).write_text(".a { color: #fff; }\n")
    assert _run(root) == 1  # fell below the baseline: must be lowered
    assert _run(root, "--tighten") == 0
    assert _run(root) == 0

    (root / css).write_text(".a { color: var(--text-color); }\n")
    assert _run(root) == 1  # stale entry
    assert "stale baseline entry" in capsys.readouterr().err
    assert _run(root, "--tighten") == 0


@pytest.mark.unit
def test_new_file_with_a_literal_fails_and_a_clean_file_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"frontend/src/components/a.css": ".a { color: var(--x); }\n"})
    assert _run(root) == 0
    (root / "frontend/src/components/b.css").write_text(".b { padding: 8px; }\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert _run(root) == 1


@pytest.mark.unit
def test_unreadable_input_exits_2(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"frontend/src/components/a.css": b"\xff\xfe not utf-8 \x80"})
    assert _run(root) == 2


@pytest.mark.unit
def test_unbalanced_file_exits_2(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"frontend/src/components/a.css": ".a { color: red;\n"})
    assert _run(root) == 2


@pytest.mark.unit
def test_write_baseline_requires_a_real_reason(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"frontend/src/components/a.css": ".a { color: #fff; }\n"})
    assert _run(root, "--write-baseline", "--reason", "TODO") == 2


@pytest.mark.unit
def test_the_real_tree_is_within_its_baseline() -> None:
    """The committed baseline matches the committed tree (the hook's own question)."""
    assert guard.main(["--root", str(_REPO)]) == 0


@pytest.mark.unit
def test_hook_mode_rewrites_lowered_baseline_but_never_on_growth(tmp_path: Path) -> None:
    css = "frontend/src/components/a.css"
    root = _repo(tmp_path, {css: ".a { color: #fff; background: #000; }\n"})
    assert _run(root, "--write-baseline", "--reason", REASON) == 0
    base = root / "base.txt"

    (root / css).write_text(".a { color: #fff; background: #000; border-color: #111; }\n")
    grown = base.read_text("utf-8")
    assert _run(root) == 1
    assert base.read_text("utf-8") == grown

    (root / css).write_text(".a { color: #fff; }\n")
    assert _run(root) == 1
    assert base.read_text("utf-8") != grown
    assert _run(root) == 0
