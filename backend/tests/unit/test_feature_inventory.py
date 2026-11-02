"""Tests for scripts/feature_inventory*.py: diff/accept semantics and a cheap extraction smoke."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _load(name: str):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(SCRIPTS))


inv_diff = _load("feature_inventory_diff")
inv = _load("feature_inventory")

OLD = {"api.routes": {"GET /a": {"name": "a"}, "GET /b": {"name": "b"}}, "ws.events": {"x": 1}}


def _write(tmp_path, name, obj):
    import json

    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return str(p)


def _run(tmp_path, old, new, accept_text=None):
    args = ["diff", _write(tmp_path, "a.json", old), _write(tmp_path, "b.json", new)]
    if accept_text is not None:
        acc = tmp_path / "accept.tsv"
        acc.write_text(accept_text)
        args += ["--accept", str(acc)]
    return inv.main(args)


def test_identical_snapshots_pass(tmp_path):
    assert _run(tmp_path, OLD, OLD) == 0


def test_unlisted_removed_route_fails(tmp_path):
    new = {**OLD, "api.routes": {"GET /a": {"name": "a"}}}
    assert _run(tmp_path, OLD, new) == 1


def test_listed_removed_route_passes(tmp_path):
    new = {**OLD, "api.routes": {"GET /a": {"name": "a"}}}
    assert _run(tmp_path, OLD, new, "api.routes\tGET /b\tmoved to /c in PR 12\n") == 0


def test_accept_entry_covers_only_its_own_key(tmp_path):
    new = {**OLD, "api.routes": {}}
    assert _run(tmp_path, OLD, new, "api.routes\tGET /b\tintended\n") == 1


def test_accept_for_other_section_does_not_apply(tmp_path):
    new = {**OLD, "api.routes": {"GET /a": {"name": "a"}}}
    assert _run(tmp_path, OLD, new, "ws.events\tGET /b\twrong section\n") == 1


def test_prefix_glob_accept(tmp_path):
    new = {**OLD, "api.routes": {}}
    assert _run(tmp_path, OLD, new, "api.routes\tGET /*\tall renamed\n") == 0


def test_missing_reason_rejected(tmp_path):
    new = {**OLD, "api.routes": {"GET /a": {"name": "a"}}}
    assert _run(tmp_path, OLD, new, "api.routes\tGET /b\t\n") == 2
    with pytest.raises(inv_diff.AcceptError):
        acc = tmp_path / "bad.tsv"
        acc.write_text("api.routes\tGET /b\n")
        inv_diff.load_accept(acc)


def test_added_and_changed_are_reported_and_fail(tmp_path):
    new = {
        "api.routes": {"GET /a": {"name": "renamed"}, "GET /b": {"name": "b"}, "GET /c": {}},
        "ws.events": {"x": 1},
    }
    diffs = inv_diff.compute_diff(OLD, new)
    assert {(d.key, d.kind) for d in diffs} == {("GET /a", "changed"), ("GET /c", "added")}
    assert _run(tmp_path, OLD, new) == 1


def test_missing_section_is_a_difference(tmp_path):
    assert _run(tmp_path, OLD, {"api.routes": OLD["api.routes"]}) == 1


def test_parse_shell_case_reads_only_dispatch_level():
    text = 'case "$1" in\n  start|up)\n    case "$x" in\n      --inner) ;;\n    esac\n    ;;\n  stop)\n    ;;\n  *) ;;\nesac\n'
    assert set(inv.parse_shell_case(text)) == {"start", "up", "stop"}


def test_extraction_smoke_static_sections():
    root = Path(__file__).resolve().parents[3]
    cli = inv.cli_sections(root)
    assert "cmd:start" in cli["cli.opentr"]
    assert "stage:preflight" in cli["cli.release"] and "stage:finish" in cli["cli.release"]
    fe = inv.frontend_sections(root)
    assert "en" in fe["frontend.i18n_counts"] and fe["frontend.i18n_counts"]["en"] > 100
    assert any(r.startswith("+layout") for r in fe["frontend.routes"])


def test_unimportable_app_module_fails_snapshot_instead_of_being_omitted(tmp_path):
    backend = tmp_path / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "app" / "__init__.py").write_text("")
    (backend / "app" / "main.py").write_text("raise RuntimeError('planted-import-failure')\n")
    with pytest.raises(RuntimeError, match="planted-import-failure"):
        inv.backend_sections(tmp_path)
