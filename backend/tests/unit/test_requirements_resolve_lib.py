"""scripts/lib/requirements_resolve.py: the pip dry-run machinery shared by two scripts.

``check-requirements-resolve.py`` (does it resolve, do the variants agree) and
``dependency-diff.py`` (what is the closure at two refs) must read one report the same way, so
the reading lives in one module and these tests pin it. pip itself needs the network, so
``subprocess.run`` is replaced by a stand-in that writes a fixture report.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import requirements_resolve as rr

SCRIPT = ROOT / "scripts" / "check-requirements-resolve.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_requirements_resolve_lib_use", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_script()


def _report(**versions: str) -> dict[str, object]:
    return {"install": [{"metadata": {"name": n, "version": v}} for n, v in versions.items()]}


def test_resolved_versions_normalises_names() -> None:
    report = _report(**{"pyannote.audio": "4.0.7", "Typing_Extensions": "4.15.0"})
    assert rr.resolved_versions(report) == {
        "pyannote-audio": "4.0.7",
        "typing-extensions": "4.15.0",
    }


def test_resolved_versions_rejects_a_report_with_no_install_list() -> None:
    with pytest.raises(ValueError, match="install"):
        rr.resolved_versions({"version": "1"})


def test_resolved_packages_rejects_a_report_with_no_install_list() -> None:
    with pytest.raises(ValueError, match="install"):
        rr.resolved_packages({"version": "1"})


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"license_expression": "MIT", "license": "Apache-2.0"}, "MIT"),
        ({"license_expression": "  ", "license": "Apache-2.0"}, "Apache-2.0"),
        ({"license": "", "classifier": ["License :: OSI Approved :: BSD License"]}, "BSD License"),
        (
            {
                "classifier": [
                    "License :: OSI Approved :: MIT License",
                    "License :: OSI Approved :: Apache Software License",
                    "Programming Language :: Python :: 3",
                ]
            },
            "Apache Software License OR MIT License",
        ),
        ({"classifier": ["Programming Language :: Python"]}, None),
        ({}, None),
    ],
)
def test_report_license_precedence(meta: dict[str, object], expected: str | None) -> None:
    assert rr.report_license(meta) == expected


def test_resolved_packages_carries_version_and_license() -> None:
    report: dict[str, object] = {
        "install": [
            {"metadata": {"name": "Foo_Bar", "version": "1.2.3", "license_expression": "MIT"}},
            {"metadata": {"name": "baz", "version": "0.1"}},
        ]
    }
    assert rr.resolved_packages(report) == {
        "foo-bar": rr.ResolvedPackage("1.2.3", "MIT"),
        "baz": rr.ResolvedPackage("0.1", None),
    }


def test_base_version_drops_only_the_local_tag() -> None:
    assert rr.base_version("2.11.0+cu128") == "2.11.0"
    assert rr.base_version("2.11.0") == "2.11.0"


class _FakePip:
    """Stands in for ``subprocess.run``: records the call, writes the report pip would."""

    def __init__(self, returncode: int = 0, output: str = "") -> None:
        self.returncode, self.output = returncode, output
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        if self.returncode == 0:
            Path(command[command.index("--report") + 1]).write_text(
                json.dumps(_report(requests="2.0.0"))
            )
        return subprocess.CompletedProcess(command, self.returncode, self.output, "")


def test_resolve_runs_pip_for_the_given_backend_and_keeps_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakePip()
    monkeypatch.setattr(rr.subprocess, "run", fake)
    backend = tmp_path / "other" / "backend"
    outcome = rr.resolve("requirements-nodeps.txt", ("--no-deps",), tmp_path, backend)
    ((command, kwargs),) = fake.calls
    assert outcome == rr.Outcome("requirements-nodeps.txt", True, {"requests": "2.0.0"}, "")
    assert "--no-deps" in command and "--dry-run" in command
    assert command[-2:] == ["-r", str(backend / "requirements-nodeps.txt")]
    assert kwargs["cwd"] == backend
    assert (tmp_path / "requirements-nodeps.txt.json").is_file()


def test_resolve_failure_returns_the_conflict_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    noise = "\n".join(f"line {i}" for i in range(40))
    fake = _FakePip(1, noise + "\nERROR: conflict A\nbecause B")
    monkeypatch.setattr(rr.subprocess, "run", fake)
    outcome = rr.resolve("requirements.txt", (), tmp_path)
    assert not outcome.ok and outcome.resolved == {}
    assert outcome.detail.startswith("ERROR: conflict A")
    assert "line 3" not in outcome.detail


def test_steps_keep_the_reference_first_and_nodeps_role() -> None:
    assert rr.STEPS[0] == ("requirements.txt", ())
    assert dict(rr.STEPS)["requirements-nodeps.txt"] == ("--no-deps",)


# --- the script that now imports it ----------------------------------------------------------


def test_script_uses_the_shared_resolve_and_keeps_no_copy() -> None:
    tree = ast.parse(SCRIPT.read_text())
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.ClassDef)}
    assert not defined & {"resolve", "resolved_versions", "normalise", "base_version", "Outcome"}
    assert check.resolve is rr.resolve
    assert check.STEPS is rr.STEPS


def test_report_dir_keeps_the_pip_reports_and_backend_dir_is_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = []

    def fake_resolve(name: str, extra: tuple[str, ...], workdir: Path, backend: Path) -> rr.Outcome:
        seen.append((name, extra, workdir, backend))
        (workdir / f"{name}.json").write_text(json.dumps(_report(a="1")))
        return rr.Outcome(name, True, {"a": "1"}, "")

    monkeypatch.setattr(check, "resolve", fake_resolve)
    reports, backend = tmp_path / "reports", tmp_path / "wt" / "backend"
    code = check.main(
        [
            "--only",
            "requirements-nodeps.txt",
            "--report-dir",
            str(reports),
            "--backend-dir",
            str(backend),
        ]
    )
    assert code == 0, capsys.readouterr().out
    assert seen == [("requirements-nodeps.txt", ("--no-deps",), reports, backend)]
    assert (reports / "requirements-nodeps.txt.json").is_file()


def test_without_report_dir_the_reports_do_not_outlive_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = []

    def fake_resolve(name: str, extra: tuple[str, ...], workdir: Path, backend: Path) -> rr.Outcome:
        (workdir / f"{name}.json").write_text("{}")
        kept.append(workdir)
        assert backend == rr.BACKEND
        return rr.Outcome(name, True, {}, "")

    monkeypatch.setattr(check, "resolve", fake_resolve)
    assert check.main(["--only", "requirements-nodeps.txt"]) == 0
    assert kept and not kept[0].exists()


# --- relative paths: pip runs with cwd=backend, so every path handed to it must be absolute ---


class _CwdAwarePip:
    """Like pip: resolves relative `--report` / `-r` paths against the `cwd` it was given.

    A stub that ignores `cwd` is how a relative `--report-dir` once passed every test and then
    failed on its first real run (the report landed in `backend/<dir>/`, which does not exist).
    """

    def __init__(self, versions: dict[str, dict[str, str]] | None = None) -> None:
        self.versions = versions or {}
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "pip 25.0 from /x (python 3.13)", "")
        self.commands.append(command)
        cwd = Path(str(kwargs["cwd"]))
        requirements = cwd / command[-1]
        if not requirements.is_file():
            return subprocess.CompletedProcess(command, 1, "", f"Could not open {requirements}")
        report = cwd / command[command.index("--report") + 1]
        if not report.parent.is_dir():
            return subprocess.CompletedProcess(command, 1, "", f"No such directory {report}")
        report.write_text(json.dumps(_report(**self.versions.get(requirements.name, {"a": "1"}))))
        return subprocess.CompletedProcess(command, 0, "", "")


def test_resolve_hands_pip_an_absolute_report_path_for_a_relative_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _CwdAwarePip()
    monkeypatch.setattr(rr.subprocess, "run", fake)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "requirements.txt").write_text("a==1\n")
    (tmp_path / "reports").mkdir()
    monkeypatch.chdir(tmp_path)
    outcome = rr.resolve("requirements.txt", (), Path("reports"), Path("backend"))
    assert outcome.ok, outcome.detail
    (command,) = fake.commands
    assert Path(command[command.index("--report") + 1]).is_absolute()
    assert Path(command[-1]).is_absolute()
    assert (tmp_path / "reports" / "requirements.txt.json").is_file()


def test_script_works_with_relative_report_dir_and_backend_dir_from_another_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _CwdAwarePip()
    monkeypatch.setattr(rr.subprocess, "run", fake)
    scratch = tmp_path / "scratch"
    (scratch / "wt" / "backend").mkdir(parents=True)
    for name in ("requirements.txt", "requirements-nodeps.txt"):
        (scratch / "wt" / "backend" / name).write_text("a==1\n")
    monkeypatch.chdir(scratch)
    code = check.main(
        [
            "--only",
            "requirements.txt",
            "--only",
            "requirements-nodeps.txt",
            "--report-dir",
            "pipreports",
            "--backend-dir",
            "wt/backend",
        ]  # fmt: skip
    )
    assert code == 0, capsys.readouterr().out
    for name in ("requirements.txt", "requirements-nodeps.txt"):
        assert (scratch / "pipreports" / f"{name}.json").is_file()
    assert not (scratch / "wt" / "backend" / "pipreports").exists()


def _drifting_pip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    fake = _CwdAwarePip(
        {"requirements.txt": {"anyio": "4.15.1"}, "requirements-lite.txt": {"anyio": "4.16.0"}}
    )
    monkeypatch.setattr(rr.subprocess, "run", fake)
    backend = tmp_path / "backend"
    backend.mkdir()
    for name in ("requirements.txt", "requirements-lite.txt"):
        (backend / name).write_text("anyio\n")
    return ["--only", "requirements-lite.txt", "--backend-dir", str(backend)]


def test_lite_drift_fails_the_run_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert check.main(_drifting_pip(monkeypatch, tmp_path)) == 1


def test_no_drift_check_resolves_without_comparing_lite_to_prod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _drifting_pip(monkeypatch, tmp_path)
    assert check.main([*args, "--no-drift-check"]) == 0
    out = capsys.readouterr().out
    assert "anyio" not in out
    assert (tmp_path / "backend" / "requirements-lite.txt").is_file()
