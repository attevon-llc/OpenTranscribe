"""scripts/check-licenses.py and scripts/lib/license_gate.py.

Fixtures are built in ``tmp_path``: npm lockfiles (v3), pip ``--report`` JSON, an allowlist and
a stand-in report/VERSION pair. The CLI's ``main`` is run for the end-to-end cases so the exit
codes (0 pass, 1 policy violation, 2 unusable input) and the evidence files are what is asserted.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import license_gate as gate
import requirements_resolve as resolve

TAB = "\t"


def _load_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_licenses_under_test", ROOT / "scripts" / "check-licenses.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


# --- helpers -------------------------------------------------------------------------------


def _lock(path: Path, entries: Mapping[str, Mapping[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"name": "app", "lockfileVersion": 3, "packages": {"": {"name": "app"}, **entries}}
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _pip_report(path: Path, items: Sequence[Mapping[str, object]]) -> Path:
    install = [{"metadata": {"name": m["name"], "version": "1.0", **m}} for m in items]
    path.write_text(json.dumps({"version": "1", "install": install}), encoding="utf-8")
    return path


def _allowlist(path: Path, *lines: str) -> Path:
    path.write_text("# header\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def _entry(
    eco_name: str, license_text: str, reason: str = "reviewed the licence file by hand"
) -> str:
    return TAB.join([eco_name, license_text, reason])


def _run(tmp_path: Path, *argv: str) -> int:
    return int(cli.main(["--root", str(tmp_path), "--out-dir", str(tmp_path / "out"), *argv]))


def _frontend(tmp_path: Path, entries: Mapping[str, Mapping[str, object]]) -> Path:
    return _lock(tmp_path / "frontend" / "package-lock.json", entries)


def _npm_run(tmp_path: Path, allow: Path | None = None) -> int:
    allowlist = allow or _allowlist(tmp_path / "allow.txt")
    return _run(tmp_path, "--npm-only", "--allowlist", str(allowlist))


def _evidence(tmp_path: Path) -> dict[str, object]:
    loaded = json.loads((tmp_path / "out" / "license-gate.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _rows(tmp_path: Path, key: str) -> list[dict[str, object]]:
    rows = _evidence(tmp_path)[key]
    assert isinstance(rows, list)
    return rows


# --- expression classification -------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("MIT", "allow"),
        ("(MIT OR Apache-2.0)", "allow"),
        ("MIT AND Zlib", "allow"),
        ("MPL-2.0", "concern"),
        ("MPL-2.0 AND MIT", "concern"),
        ("GPL-3.0-only", "blocker"),
        ("GPL-2.0+", "blocker"),
        ("MIT WITH Commons-Clause", "blocker"),
        ("Apache-2.0 WITH Commons-Clause", "blocker"),
        ("Apache-2.0 WITH LLVM-exception", "allow"),
        ("(GPL-2.0-only WITH Classpath-exception-2.0) OR MIT", "allow"),
        ("(Apache-2.0 WITH LLVM-exception) AND GPL-3.0-only", "blocker"),
        ("(MIT OR Apache-2.0) AND GPL-3.0-only", "blocker"),
        ("MIT OR (GPL-3.0-only AND AGPL-3.0-only)", "allow"),
        ("GPL-3.0-only OR AGPL-3.0-only", "blocker"),
        ("GNU General Public License v2 or later (GPLv2+)", "blocker"),
        ("MIT License", "allow"),
        ("Apache Software License", "allow"),
        ("Other/Proprietary License", "proprietary"),
        ("LicenseRef-NVIDIA-Proprietary", "proprietary"),
        ("UNKNOWN", "unknown"),
        ("NOASSERTION", "unknown"),
        ("", "unknown"),
        ("   ", "unknown"),
        ("Totally Made Up License 9", "unknown"),
        ("(MIT OR", "unknown"),
        ("MIT)", "unknown"),
        ("MIT WITH", "unknown"),
    ],
)
def test_expression_classification(expr: str, expected: str) -> None:
    assert gate.classify(expr)[0] == expected


def test_classify_none_is_unknown_not_allow() -> None:
    assert gate.classify(None)[0] == "unknown"


def test_semicolon_list_is_and_the_conservative_reading() -> None:
    assert gate._as_expression("MIT;GPL-3.0-only") == "MIT AND GPL-3.0-only"
    assert gate.classify(gate._as_expression("MIT;GPL-3.0-only"))[0] == "blocker"


# --- npm lockfile --------------------------------------------------------------------------


def test_npm_lock_reads_string_spdx_array_object_and_skips_dev_and_links(tmp_path: Path) -> None:
    lock = _lock(
        tmp_path / "package-lock.json",
        {
            "node_modules/a": {"version": "1.0.0", "license": "MIT"},
            "node_modules/b": {"version": "1.0.0", "license": "(MIT OR Apache-2.0)"},
            "node_modules/c": {"version": "1.0.0", "license": ["MIT", "ISC"]},
            "node_modules/d": {"version": "1.0.0", "license": {"type": "BSD-3-Clause"}},
            "node_modules/dev-only": {"version": "1.0.0", "dev": True, "license": "GPL-3.0-only"},
            "node_modules/linked": {"resolved": "../x", "link": True},
            "node_modules/@scope/e": {"version": "2.0.0", "license": "ISC"},
            "node_modules/x/node_modules/f": {"version": "3.0.0", "license": "0BSD"},
        },
    )
    got = {p.name: p.license for p in gate.load_npm_lock(lock, "frontend")}
    assert got == {
        "a": "MIT",
        "b": "(MIT OR Apache-2.0)",
        "c": "MIT;ISC",
        "d": "BSD-3-Clause",
        "@scope/e": "ISC",
        "f": "0BSD",
    }


def test_npm_lock_falls_back_to_installed_package_json(tmp_path: Path) -> None:
    lock = _lock(tmp_path / "package-lock.json", {"node_modules/p": {"version": "1.0.0"}})
    manifest = tmp_path / "node_modules" / "p" / "package.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "p", "license": "MIT"}), encoding="utf-8")
    (pkg,) = gate.load_npm_lock(lock, "frontend")
    assert (pkg.license, pkg.source) == ("MIT", "node_modules")


def test_npm_lock_without_any_licence_is_missing_and_classifies_unknown(tmp_path: Path) -> None:
    lock = _lock(tmp_path / "package-lock.json", {"node_modules/p": {"version": "1.0.0"}})
    (pkg,) = gate.load_npm_lock(lock, "frontend")
    assert (pkg.license, pkg.source) == ("", "missing")
    assert gate.classify(pkg.license)[0] == "unknown"


@pytest.mark.parametrize(
    "body",
    [
        {"lockfileVersion": 3, "packages": {"": {}}},  # only the project itself
        {"lockfileVersion": 3, "packages": {}},
        {"lockfileVersion": 1, "dependencies": {"a": {"version": "1"}}},
        {
            "lockfileVersion": 3,
            "packages": {"": {}, "node_modules/d": {"dev": True, "version": "1"}},
        },
    ],
)
def test_npm_lock_with_nothing_to_judge_is_unusable(
    tmp_path: Path, body: dict[str, object]
) -> None:
    path = tmp_path / "package-lock.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(gate.InputError):
        gate.load_npm_lock(path, "frontend")


# --- pip report ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("meta", "expected_text", "expected_source", "expected_class"),
    [
        (
            {"license_expression": "MIT", "license": "GPL-3.0-only"},
            "MIT",
            "license_expression",
            "allow",
        ),
        ({"license": "Apache-2.0"}, "Apache-2.0", "license", "allow"),
        (
            {"license": "UNKNOWN", "classifier": ["License :: OSI Approved :: MIT License"]},
            "MIT",
            "classifier",
            "allow",
        ),
        (
            {"classifier": ["License :: OSI Approved :: Apache Software License"]},
            "Apache-2.0",
            "classifier",
            "allow",
        ),
        (
            {"classifier": ["License :: Other/Proprietary License"]},
            "Other/Proprietary License",
            "classifier",
            "proprietary",
        ),
        (
            {"license": "GPL-3.0-only", "classifier": ["License :: OSI Approved :: MIT License"]},
            "GPL-3.0-only",
            "license",
            "blocker",
        ),
        (
            {
                "license": "BSD 2-Clause License",
                "classifier": ["License :: OSI Approved :: BSD License"],
            },
            "BSD 2-Clause License",
            "license",
            "allow",
        ),
        ({}, "", "missing", "unknown"),
        ({"license": "UNKNOWN"}, "UNKNOWN", "license", "unknown"),
        ({"classifier": ["License :: OSI Approved"]}, "", "missing", "unknown"),
    ],
)
def test_python_license_sources(
    meta: dict[str, object], expected_text: str, expected_source: str, expected_class: str
) -> None:
    text, source = gate.python_license(meta)
    assert (text, source) == (expected_text, expected_source)
    assert gate.classify(text)[0] == expected_class


def test_python_license_body_is_keyed_by_digest_and_fails_closed() -> None:
    body = "BSD 3-Clause License\n\nCopyright 2024 X\n\nRedistribution and use ..."
    text, source = gate.python_license({"license": body})
    assert text.startswith("BSD 3-Clause License [licence text sha256:")
    assert source == "license"
    assert gate.classify(text)[0] == "unknown"
    other, _ = gate.python_license({"license": body + " changed"})
    assert other != text


def test_python_license_agrees_with_the_dependency_diff_resolver_on_identifier_fields() -> None:
    for meta in (
        {"license_expression": "MIT", "license": "other"},
        {"license": "Apache-2.0"},
        {"license_expression": "GPL-2.0-or-later"},
    ):
        assert gate.python_license(meta)[0] == resolve.report_license(meta)


def test_pip_report_must_have_an_install_list(tmp_path: Path) -> None:
    bad = tmp_path / "r.json"
    bad.write_text(json.dumps({"install": []}), encoding="utf-8")
    with pytest.raises(gate.InputError):
        gate.load_pip_report(bad)
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(gate.InputError):
        gate.load_pip_report(bad)


# --- the gate end to end -------------------------------------------------------------------

CLEAN_NPM = {"node_modules/a": {"version": "1.0.0", "license": "MIT"}}


def test_clean_tree_passes_and_writes_evidence(tmp_path: Path) -> None:
    _frontend(tmp_path, {**CLEAN_NPM, "node_modules/l": {"version": "1.0.0", "license": "MPL-2.0"}})
    assert _npm_run(tmp_path) == 0
    evidence = _evidence(tmp_path)
    assert evidence["status"] == "pass"
    assert evidence["counts"] == {
        "npm/frontend": {"allow": 1, "concern": 1, "proprietary": 0, "unknown": 0, "blocker": 0}
    }
    assert [c["name"] for c in _rows(tmp_path, "concerns")] == ["l"]
    for name in ("license-gate.md", "license-gate.csv"):
        assert (tmp_path / "out" / name).read_text(encoding="utf-8").strip()
    assert "npm:l" in (tmp_path / "out" / "license-gate.md").read_text(encoding="utf-8")


def test_planted_copyleft_package_fails_and_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _frontend(
        tmp_path,
        {**CLEAN_NPM, "node_modules/planted-gpl": {"version": "1.0.0", "license": "GPL-3.0-only"}},
    )
    assert _npm_run(tmp_path) == 1
    assert "planted-gpl" in capsys.readouterr().err
    assert _evidence(tmp_path)["status"] == "fail"


def test_unknown_and_missing_licences_fail_closed(tmp_path: Path) -> None:
    _frontend(tmp_path, {**CLEAN_NPM, "node_modules/nolic": {"version": "1.0.0"}})
    assert _npm_run(tmp_path) == 1
    _frontend(
        tmp_path,
        {**CLEAN_NPM, "node_modules/odd": {"version": "1.0.0", "license": "SEE LICENSE IN FILE"}},
    )
    assert _npm_run(tmp_path) == 1


def test_with_commons_clause_rider_is_not_waved_through(tmp_path: Path) -> None:
    _frontend(
        tmp_path, {"node_modules/cc": {"version": "1.0.0", "license": "MIT WITH Commons-Clause"}}
    )
    assert _npm_run(tmp_path) == 1


@pytest.mark.parametrize(
    "entries", [{}, {"node_modules/d": {"version": "1", "dev": True, "license": "MIT"}}]
)
def test_empty_input_is_unusable_not_a_pass(
    tmp_path: Path, entries: Mapping[str, Mapping[str, object]]
) -> None:
    _frontend(tmp_path, entries)
    assert _npm_run(tmp_path) == 2


def test_evaluating_zero_packages_is_unusable_not_a_pass() -> None:
    with pytest.raises(gate.InputError):
        gate.evaluate([], {}, ["npm"])


def test_missing_lockfile_and_unparseable_lockfile_are_unusable(tmp_path: Path) -> None:
    assert _npm_run(tmp_path) == 2
    path = tmp_path / "frontend" / "package-lock.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    assert _npm_run(tmp_path) == 2


def test_missing_allowlist_is_unusable(tmp_path: Path) -> None:
    _frontend(tmp_path, CLEAN_NPM)
    assert _run(tmp_path, "--npm-only", "--allowlist", str(tmp_path / "nope.txt")) == 2


def test_python_leg_needs_reports(tmp_path: Path) -> None:
    _frontend(tmp_path, CLEAN_NPM)
    assert _run(tmp_path, "--allowlist", str(_allowlist(tmp_path / "a.txt"))) == 2
    empty = tmp_path / "reports"
    empty.mkdir()
    assert (
        _run(tmp_path, "--pip-report-dir", str(empty), "--allowlist", str(tmp_path / "a.txt")) == 2
    )


def test_python_leg_judges_transitives_and_classifiers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _frontend(tmp_path, CLEAN_NPM)
    reports = tmp_path / "reports"
    reports.mkdir()
    _pip_report(
        reports / "requirements.txt.json",
        [
            {"name": "good", "license_expression": "MIT"},
            {
                "name": "viaclassifier",
                "license": "UNKNOWN",
                "classifier": ["License :: OSI Approved :: MIT License"],
            },
            {"name": "transitive-gpl", "license_expression": "GPL-2.0-or-later"},
            {"name": "nothing"},
        ],
    )
    allow = _allowlist(tmp_path / "a.txt")
    assert _run(tmp_path, "--pip-report-dir", str(reports), "--allowlist", str(allow)) == 1
    err = capsys.readouterr().err
    assert "transitive-gpl" in err and "pip:nothing" in err
    assert "viaclassifier" not in err and "pip:good" not in err


def test_all_three_shipped_reports_are_read_and_deduplicated(tmp_path: Path) -> None:
    _frontend(tmp_path, CLEAN_NPM)
    reports = tmp_path / "reports"
    reports.mkdir()
    same = [{"name": "dup", "license_expression": "MIT"}]
    _pip_report(reports / "requirements.txt.json", same)
    _pip_report(
        reports / "requirements-lite.txt.json",
        [*same, {"name": "liteonly", "license_expression": "ISC"}],
    )
    _pip_report(
        reports / "requirements-dev.txt.json",
        [{"name": "devonly", "license_expression": "GPL-3.0-only"}],
    )
    assert (
        _run(
            tmp_path,
            "--pip-report-dir",
            str(reports),
            "--allowlist",
            str(_allowlist(tmp_path / "a.txt")),
        )
        == 0
    )
    names = {row["name"] for row in _rows(tmp_path, "packages")}
    assert names == {"a", "dup", "liteonly"}


# --- allowlist -----------------------------------------------------------------------------


def test_allowlist_approves_exactly_that_name_and_licence_and_shows_the_reason(
    tmp_path: Path,
) -> None:
    _frontend(tmp_path, {"node_modules/gpl": {"version": "1.0.0", "license": "GPL-2.0-or-later"}})
    allow = _allowlist(
        tmp_path / "a.txt", _entry("npm:gpl", "GPL-2.0-or-later", "documented exception for tests")
    )
    assert _npm_run(tmp_path, allow) == 0
    (exception,) = _rows(tmp_path, "allowlisted")
    assert exception["allowlist_reason"] == "documented exception for tests"
    assert exception["class"] == "BLOCKER"
    assert "documented exception for tests" in (tmp_path / "out" / "license-gate.md").read_text(
        encoding="utf-8"
    )


def test_relicensed_package_is_not_covered_by_the_old_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    allow = _allowlist(tmp_path / "a.txt", _entry("npm:gpl", "GPL-2.0-or-later"))
    _frontend(tmp_path, {"node_modules/gpl": {"version": "2.0.0", "license": "AGPL-3.0-only"}})
    assert _npm_run(tmp_path, allow) == 1
    err = capsys.readouterr().err
    assert "BLOCKER: npm:gpl" in err  # the new licence is a fresh violation
    assert "STALE allowlist line npm:gpl" in err  # and the old approval no longer matches


def test_stale_allowlist_line_for_a_removed_package_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _frontend(tmp_path, CLEAN_NPM)
    allow = _allowlist(tmp_path / "a.txt", _entry("npm:gone", "GPL-3.0-only"))
    assert _npm_run(tmp_path, allow) == 1
    assert "STALE allowlist line npm:gone" in capsys.readouterr().err


def test_allowlist_line_for_a_package_that_is_now_fine_is_stale(tmp_path: Path) -> None:
    _frontend(tmp_path, {"node_modules/a": {"version": "1.0.0", "license": "MIT"}})
    allow = _allowlist(tmp_path / "a.txt", _entry("npm:a", "MIT"))
    assert _npm_run(tmp_path, allow) == 1


def test_npm_only_does_not_call_python_entries_stale(tmp_path: Path) -> None:
    _frontend(tmp_path, CLEAN_NPM)
    allow = _allowlist(tmp_path / "a.txt", _entry("pip:mutagen", "GPL-2.0-or-later"))
    assert _npm_run(tmp_path, allow) == 0


def test_python_names_match_by_pep_503_and_licence_string_whitespace(tmp_path: Path) -> None:
    _frontend(tmp_path, CLEAN_NPM)
    reports = tmp_path / "reports"
    reports.mkdir()
    _pip_report(
        reports / "requirements.txt.json", [{"name": "Foo_Bar.baz", "license": "GPL-2.0-or-later"}]
    )
    allow = _allowlist(tmp_path / "a.txt", _entry("pip:foo-bar-baz", "GPL-2.0-or-later"))
    assert _run(tmp_path, "--pip-report-dir", str(reports), "--allowlist", str(allow)) == 0


def test_python_package_without_any_licence_is_approved_with_the_none_spelling(
    tmp_path: Path,
) -> None:
    _frontend(tmp_path, CLEAN_NPM)
    reports = tmp_path / "reports"
    reports.mkdir()
    _pip_report(reports / "requirements.txt.json", [{"name": "bare"}])
    allow = _allowlist(tmp_path / "a.txt", _entry("pip:bare", "(none)"))
    assert _run(tmp_path, "--pip-report-dir", str(reports), "--allowlist", str(allow)) == 0


@pytest.mark.parametrize(
    "line",
    [
        TAB.join(["npm:x", "GPL-3.0-only", ""]),  # no reason
        TAB.join(["npm:x", "GPL-3.0-only", "todo"]),  # placeholder reason
        TAB.join(["npm:x", "GPL-3.0-only"]),  # two fields
        TAB.join(["cargo:x", "GPL-3.0-only", "a real sounding reason"]),  # unknown ecosystem
        TAB.join(["npm", "GPL-3.0-only", "a real sounding reason"]),  # no name
    ],
)
def test_malformed_or_reasonless_allowlist_lines_are_unusable(tmp_path: Path, line: str) -> None:
    _frontend(tmp_path, CLEAN_NPM)
    allow = tmp_path / "a.txt"
    allow.write_text(line + "\n", encoding="utf-8")
    assert _npm_run(tmp_path, allow) == 2


def test_duplicate_allowlist_lines_are_unusable(tmp_path: Path) -> None:
    _frontend(tmp_path, CLEAN_NPM)
    allow = _allowlist(
        tmp_path / "a.txt", _entry("npm:x", "GPL-3.0-only"), _entry("npm:X", "GPL-3.0-only")
    )
    assert _npm_run(tmp_path, allow) == 2


def test_the_committed_allowlist_parses_and_every_line_has_a_reason() -> None:
    entries = gate.load_allowlist(ROOT / "scripts" / "licenses-allowlist.txt")
    assert ("pip", "mutagen", "GPL-2.0-or-later") in entries
    assert "permissive-only policy exception" in entries[("pip", "mutagen", "GPL-2.0-or-later")]


# --- report version ------------------------------------------------------------------------


def _report(tmp_path: Path, scope: str, version: str = "v0.5.1\n") -> tuple[Path, Path]:
    summary = tmp_path / "SBOM-SUMMARY.md"
    summary.write_text(
        f"# SBOM\n\nGenerated: 2026-09-24\n{scope}\n\nNext paragraph v9.9.9\n", encoding="utf-8"
    )
    version_file = tmp_path / "VERSION"
    version_file.write_text(version, encoding="utf-8")
    return summary, version_file


SCOPE = "Scope: images (`a/backend:v0.5.1`,\n`a/frontend:v0.5.1`)."


def test_report_version_matches_version_file(tmp_path: Path) -> None:
    summary, version_file = _report(tmp_path, SCOPE)
    assert gate.verify_report_version(summary, version_file) == []
    assert _run(tmp_path, "--verify-report-version", "--report", str(summary)) == 0


def test_report_version_mismatch_fails(tmp_path: Path) -> None:
    summary, _ = _report(tmp_path, SCOPE.replace("v0.5.1", "v0.5.0"))
    (tmp_path / "VERSION").write_text("v0.5.1\n", encoding="utf-8")
    assert _run(tmp_path, "--verify-report-version", "--report", str(summary)) == 1


def test_report_naming_two_versions_fails_even_if_one_matches(tmp_path: Path) -> None:
    summary, _ = _report(tmp_path, "Scope: `a:v0.5.1` and `b:v0.5.0`")
    assert _run(tmp_path, "--verify-report-version", "--report", str(summary)) == 1


def test_report_version_ignores_versions_outside_the_scope_paragraph(tmp_path: Path) -> None:
    summary, version_file = _report(tmp_path, SCOPE)
    assert "v9.9.9" in summary.read_text(encoding="utf-8")
    assert gate.verify_report_version(summary, version_file) == []


def test_report_without_scope_or_version_is_unusable(tmp_path: Path) -> None:
    summary, _ = _report(tmp_path, "Nothing here")
    assert _run(tmp_path, "--verify-report-version", "--report", str(summary)) == 2
    summary, _ = _report(tmp_path, "Scope: no tag at all")
    assert _run(tmp_path, "--verify-report-version", "--report", str(summary)) == 2


def test_verify_report_version_alone_does_not_need_lockfiles(tmp_path: Path) -> None:
    summary, _ = _report(tmp_path, SCOPE)
    assert _run(tmp_path, "--verify-report-version", "--report", str(summary)) == 0
    assert not (tmp_path / "out").exists()


# --- single policy home --------------------------------------------------------------------


def test_the_gate_does_not_carry_its_own_policy_tables() -> None:
    import ast

    for name in ("scripts/lib/license_gate.py", "scripts/check-licenses.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        defined = {
            t.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name)
        } | {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert not defined & {"ALLOW", "CONCERN", "BLOCKER_EXACT", "classify_license_string"}, name
