"""Scheduled security scanning: image jobs skip on a schedule, every scan job keeps its report.

`gh workflow run` can only fire `workflow_dispatch`, so a mistake in the `if:` guards that decide
what a *scheduled* run does is invisible until the first Monday. This test parses the workflows and
evaluates each job condition for the three event shapes with a small evaluator that supports only
the forms the workflow uses (`==`, `!=`, `&&`, `||`, parentheses, quoted strings, `github.event_name`,
`inputs.component`); anything else raises, so a new expression form cannot be silently ignored.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"

IMAGE_JOBS = ("build-and-scan-backend", "build-and-scan-frontend")
SCHEDULED_JOBS = ("dockerfile-lint", "dependency-scan", "dependency-audit")

_TOKEN_RE = re.compile(r"\s*(?:(==|!=|&&|\|\||\(|\))|'([^']*)'|([A-Za-z_][\w.]*))")
Context = dict[str, str]


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise ValueError(f"unsupported expression syntax at {expr[pos:]!r} in {expr!r}")
        if m.group(1):
            tokens.append(("op", m.group(1)))
        elif m.group(2) is not None:
            tokens.append(("str", m.group(2)))
        else:
            tokens.append(("ident", m.group(3)))
        pos = m.end()
    return tokens


def evaluate(expr: str, ctx: Context) -> bool:
    """Evaluate a GitHub `if:` expression made of ==, !=, &&, ||, parentheses and string operands."""
    tokens = _tokenize(expr)
    idx = 0

    def peek() -> tuple[str, str] | None:
        return tokens[idx] if idx < len(tokens) else None

    def take() -> tuple[str, str]:
        nonlocal idx
        tok = tokens[idx]
        idx += 1
        return tok

    def operand() -> str:
        kind, val = take()
        if kind == "str":
            return val
        if kind == "ident":
            if val not in ctx:
                raise ValueError(f"unknown context value {val!r} in {expr!r}")
            return ctx[val]
        raise ValueError(f"expected operand, got {val!r} in {expr!r}")

    def primary() -> bool:
        tok = peek()
        if tok == ("op", "("):
            take()
            value = disjunction()
            if take() != ("op", ")"):
                raise ValueError(f"unbalanced parentheses in {expr!r}")
            return value
        left = operand()
        tok = peek()
        if tok is not None and tok[0] == "op" and tok[1] in ("==", "!="):
            take()
            right = operand()
            return (left == right) if tok[1] == "==" else (left != right)
        raise ValueError(f"bare operand without comparison in {expr!r}")

    def conjunction() -> bool:
        value = primary()
        while peek() == ("op", "&&"):
            take()
            rhs = primary()
            value = value and rhs
        return value

    def disjunction() -> bool:
        value = conjunction()
        while peek() == ("op", "||"):
            take()
            rhs = conjunction()
            value = value or rhs
        return value

    result = disjunction()
    if idx != len(tokens):
        raise ValueError(f"trailing tokens in {expr!r}")
    return result


def _ctx(event: str, component: str = "") -> Context:
    return {"github.event_name": event, "inputs.component": component}


def _load(name: str) -> dict[object, object]:
    doc = yaml.safe_load((WORKFLOWS / name).read_text())
    assert isinstance(doc, dict)
    return doc


def _jobs(name: str = "security-scan.yml") -> dict[str, dict[str, object]]:
    jobs = _load(name)["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _job_runs(job: dict[str, object], ctx: Context) -> bool:
    cond = job.get("if")
    return True if cond is None else evaluate(str(cond), ctx)


def _triggers(name: str) -> dict[str, object]:
    doc = _load(name)
    # PyYAML (YAML 1.1) parses the key `on` as boolean True.
    trig = doc.get("on", doc.get(True))
    assert isinstance(trig, dict)
    return trig


# --- evaluator self-checks (a broken evaluator would make every other test vacuous) ---


def test_evaluator_truth_table() -> None:
    expr = "github.event_name == 'a' && (inputs.component == 'x' || inputs.component == 'y')"
    assert evaluate(expr, _ctx("a", "x"))
    assert evaluate(expr, _ctx("a", "y"))
    assert not evaluate(expr, _ctx("a", "z"))
    assert not evaluate(expr, _ctx("b", "x"))
    assert evaluate("github.event_name != 'b'", _ctx("a"))


def test_evaluator_rejects_unsupported_syntax() -> None:
    with pytest.raises(ValueError):
        evaluate("always()", _ctx("a"))
    with pytest.raises(ValueError):
        evaluate("contains(github.ref, 'x')", _ctx("a"))


# --- the workflow ---


@pytest.mark.parametrize("job", IMAGE_JOBS)
def test_image_jobs_skip_on_schedule_and_pr(job: str) -> None:
    j = _jobs()[job]
    assert not _job_runs(j, _ctx("schedule", ""))
    assert not _job_runs(j, _ctx("pull_request", "all"))
    assert not _job_runs(j, _ctx("schedule", "all")), (
        "a schedule must not build images even if inputs leak"
    )


@pytest.mark.parametrize(
    ("job", "component"),
    [("build-and-scan-backend", "backend"), ("build-and-scan-frontend", "frontend")],
)
def test_image_jobs_run_on_dispatch(job: str, component: str) -> None:
    j = _jobs()[job]
    assert _job_runs(j, _ctx("workflow_dispatch", "all"))
    assert _job_runs(j, _ctx("workflow_dispatch", component))
    other = "frontend" if component == "backend" else "backend"
    assert not _job_runs(j, _ctx("workflow_dispatch", other))


@pytest.mark.parametrize("job", SCHEDULED_JOBS)
@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_repo_level_jobs_run_on_schedule_and_dispatch(job: str, event: str) -> None:
    assert _job_runs(_jobs()[job], _ctx(event, "" if event == "schedule" else "all"))


def test_dockerfile_lint_does_something_on_schedule() -> None:
    steps = _jobs()["dockerfile-lint"]["steps"]
    assert isinstance(steps, list)
    gated = [s for s in steps if isinstance(s, dict) and "if" in s]
    assert gated
    assert all(evaluate(str(s["if"]), _ctx("schedule")) for s in gated)


def test_summary_job_always_runs_and_covers_every_job() -> None:
    jobs = _jobs()
    summary = jobs["summary"]
    assert summary["if"] == "always()"
    needs = summary["needs"]
    assert isinstance(needs, list)
    assert set(needs) == set(jobs) - {"summary"}


def test_schedule_trigger_and_dispatch_kept() -> None:
    trig = _triggers("security-scan.yml")
    sched = trig["schedule"]
    assert isinstance(sched, list)
    assert len(sched) == 1
    assert re.fullmatch(r"\d+ \d+ \* \* \d", sched[0]["cron"])  # weekly: fixed minute/hour/dow
    assert "workflow_dispatch" in trig
    assert "pull_request" not in trig
    assert "push" not in trig


# --- artifacts, report-only, shell, permissions ---


def _upload_steps(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return [
        s
        for s in steps
        if isinstance(s, dict) and str(s.get("uses", "")).startswith("actions/upload-artifact@")
    ]


@pytest.mark.parametrize("job", ["dependency-scan", "dependency-audit", *IMAGE_JOBS])
def test_scan_jobs_upload_an_artifact_with_retention_and_hard_missing_file(job: str) -> None:
    uploads = _upload_steps(_jobs()[job])
    assert uploads, f"{job} uploads no artifact"
    for step in uploads:
        w = step["with"]
        assert isinstance(w, dict)
        assert isinstance(w.get("retention-days"), int)
        assert w.get("if-no-files-found") == "error"


def test_scheduled_gates_are_report_only_but_dispatch_still_gates() -> None:
    steps = _jobs()["dependency-scan"]["steps"]
    assert isinstance(steps, list)
    gates = [
        s
        for s in steps
        if isinstance(s, dict)
        and isinstance(s.get("with"), dict)
        and s["with"].get("exit-code") == "1"
    ]
    assert len(gates) == 2
    for gate in gates:
        # continue-on-error must be true on a schedule and false on a dispatch.
        m = re.fullmatch(r"\$\{\{\s*(.+?)\s*\}\}", str(gate["continue-on-error"]))
        assert m
        assert evaluate(m.group(1), _ctx("schedule"))
        assert not evaluate(m.group(1), _ctx("workflow_dispatch", "all"))


def test_workflow_default_shell_is_bash_so_pipes_cannot_mask_failures() -> None:
    for name in ("security-scan.yml",):
        assert _load(name)["defaults"] == {"run": {"shell": "bash"}}


def test_security_events_write_is_job_level_only() -> None:
    for name in ("security-scan.yml", "osv-scanner.yml"):
        assert _load(name)["permissions"] == {}
        writers: set[str] = set()
        for job_name, job in _jobs(name).items():
            grants = job.get("permissions") or {}
            assert isinstance(grants, dict)
            assert all(v in ("read", "write") for v in grants.values())
            assert grants.get("contents") in (None, "read")
            if grants.get("security-events") == "write":
                writers.add(job_name)
        assert writers <= {"build-and-scan-backend", "build-and-scan-frontend", "osv-scan"}


# --- osv-scanner.yml ---


def test_osv_scanner_is_weekly_push_master_pinned_and_report_only() -> None:
    trig = _triggers("osv-scanner.yml")
    assert set(trig) == {"schedule", "push"}
    assert trig["push"] == {"branches": ["master"]}
    job = _jobs("osv-scanner.yml")["osv-scan"]
    uses = str(job["uses"])
    assert re.fullmatch(
        r"google/osv-scanner-action/\.github/workflows/osv-scanner-reusable\.yml@[0-9a-f]{40}", uses
    )
    w = job["with"]
    assert isinstance(w, dict)
    assert w["fail-on-vuln"] is False
    assert "--format" not in str(w["scan-args"]) and "--output" not in str(w["scan-args"])
    assert job["permissions"] == {"actions": "read", "contents": "read", "security-events": "write"}


# --- a crashed audit tool is a failure, never a quiet green ---


def _step_run(job: str, name_prefix: str) -> str:
    steps = _jobs()[job]["steps"]
    assert isinstance(steps, list)
    for step in steps:
        if isinstance(step, dict) and str(step.get("name", "")).startswith(name_prefix):
            return str(step["run"])
    raise AssertionError(f"no step named {name_prefix!r} in {job}")


def _run_step(
    script: str, tmp_path: Path, stubs: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a workflow `run:` body under `bash -eo pipefail` with stub tools first on PATH."""
    if shutil.which("jq") is None:
        pytest.skip("jq not installed")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool, body in stubs.items():
        path = bindir / tool
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)
    work = tmp_path / "work"
    (work / "backend").mkdir(parents=True)
    (work / "backend" / "requirements.txt").write_text("requests==2.19.0\n")
    (work / "frontend").mkdir()
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GITHUB_WORKSPACE": str(work),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }
    cwd = work / "frontend" if "npm" in script else work
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


_PIP_OK = (
    'out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
    'echo \'{"dependencies":[{"name":"requests","version":"2.19.0","vulns":'
    '[{"id":"PYSEC-1","fix_versions":["2.20.0"]}]}],"fixes":[]}\' > "$out"; exit 1'
)
_PIP_CRASH = "echo boom >&2; exit 2"
_NPM_OK = (
    '[ "$1" = ci ] && exit 0\necho \'{"metadata":{"vulnerabilities":{"low":1,"high":2}}}\'; exit 1'
)
_NPM_CRASH = '[ "$1" = ci ] && exit 0\nexit 1'  # leaves the redirect target empty


def test_pip_audit_step_passes_with_findings_and_renders_them(tmp_path: Path) -> None:
    res = _run_step(_step_run("dependency-audit", "pip-audit"), tmp_path, {"pip-audit": _PIP_OK})
    assert res.returncode == 0, res.stderr
    summary = (tmp_path / "summary.md").read_text()
    assert "requests" in summary
    assert "PYSEC-1" in summary


def test_pip_audit_step_fails_when_the_tool_writes_no_report(tmp_path: Path) -> None:
    res = _run_step(_step_run("dependency-audit", "pip-audit"), tmp_path, {"pip-audit": _PIP_CRASH})
    assert res.returncode != 0
    assert "::error::" in res.stdout


def test_npm_audit_step_passes_with_findings(tmp_path: Path) -> None:
    res = _run_step(_step_run("dependency-audit", "npm audit"), tmp_path, {"npm": _NPM_OK})
    assert res.returncode == 0, res.stderr
    assert "high: 2" in (tmp_path / "summary.md").read_text()


def test_npm_audit_step_fails_when_npm_leaves_an_empty_report(tmp_path: Path) -> None:
    res = _run_step(_step_run("dependency-audit", "npm audit"), tmp_path, {"npm": _NPM_CRASH})
    assert res.returncode != 0
    assert "::error::" in res.stdout


# --- timeouts, pinned installs, secrets in the summary ---

_REUSABLE_WORKFLOW_JOBS = {
    "osv-scan"
}  # a job that calls a reusable workflow cannot set timeout-minutes


def test_every_security_scan_job_has_a_timeout() -> None:
    missing = [n for n, j in _jobs().items() if not isinstance(j.get("timeout-minutes"), int)]
    missing += [
        n
        for n, j in _jobs("osv-scanner.yml").items()
        if n not in _REUSABLE_WORKFLOW_JOBS and not isinstance(j.get("timeout-minutes"), int)
    ]
    assert len(_jobs()) >= 6
    assert missing == []


def test_pip_installs_are_pinned_to_exact_versions() -> None:
    run = _step_run("dependency-audit", "Install")
    assert re.search(r"pip==\d+(\.\d+)+", run)
    assert re.search(r"pip-audit==\d+(\.\d+)+", run)
    assert "--upgrade" not in run


def test_pip_audit_resolves_requirements_once() -> None:
    steps = _jobs()["dependency-audit"]["steps"]
    assert isinstance(steps, list)
    runs = " ".join(str(s.get("run", "")) for s in steps if isinstance(s, dict))
    assert runs.count("pip-audit -r") == 1


def test_trivy_summary_counts_secrets_as_well() -> None:
    assert ".Secrets[]?" in _step_run("dependency-scan", "Summarize Trivy")
