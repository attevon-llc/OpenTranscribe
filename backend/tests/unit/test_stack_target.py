"""Behaviour of scripts/lib/stack_target.py against a fake docker.

The fake lists the OTHER stack's containers first, so any first-match logic picks the wrong one.
Every "must fire" case has a "must stay clean" twin; a guard that cannot fail is worse than none.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MODULE = REPO / "scripts" / "lib" / "stack_target.py"
_spec = importlib.util.spec_from_file_location("stack_target", MODULE)
assert _spec is not None and _spec.loader is not None
st = importlib.util.module_from_spec(_spec)
sys.modules["stack_target"] = st
_spec.loader.exec_module(st)
import stack_checks

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
model = json.load(open(os.environ["FAKE_DOCKER_MODEL"]))
argv = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(" ".join(argv) + "\n")
if model.get("down"):
    sys.exit(1)
cs = model["containers"]
if argv[0] == "ps":
    wanted = [argv[i + 1].split("=", 1)[1] for i, a in enumerate(argv) if a == "--filter" and argv[i + 1].startswith("label=")]
    for c in cs:
        labels = {"com.docker.compose.project": c["project"], "com.docker.compose.service": c["service"]}
        if all(w.split("=", 1)[0] in labels and labels[w.split("=", 1)[0]] == w.split("=", 1)[1] for w in wanted):
            print("\t".join([c["name"], c["project"], c["service"], c.get("state", "running"), c.get("working_dir", ""), c.get("ports", "")]))
elif argv[0] == "inspect":
    c = next(x for x in cs if x["name"] == argv[1])
    print(json.dumps([c.get("inspect", {})]))
elif argv[0] == "exec":
    c = next(x for x in cs if x["name"] == argv[1])
    rest = argv[2:]
    if rest[0] == "printenv":
        if rest[1] in c.get("env", {}):
            print(c["env"][rest[1]])
            sys.exit(0)
        sys.exit(1)
    if rest[0] == "python":
        sys.exit(c.get("migrate_rc", 0))
    script = " ".join(rest)
    for key, out in c.get("sql", {}).items():
        if key in script:
            print(out)
            sys.exit(0)
    sys.exit(1)
"""


def mk(name: str, project: str, service: str, ports: str = "", **extra: object) -> dict:
    return {"name": name, "project": project, "service": service, "ports": ports, **extra}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with a two-revision chain whose head is v471."""
    root = tmp_path / "checkout"
    versions = root / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "v468_a.py").write_text('revision = "v468_a"\ndown_revision = None\n')
    (versions / "v471_b.py").write_text('revision = "v471_b"\ndown_revision = "v468_a"\n')
    (root / "scripts").mkdir()
    (root / "scripts" / "x.sh").write_text("x")
    for cmd in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", "-C", str(root), *cmd], check=True)
    return root


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[list[dict]], Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_MODEL", str(tmp_path / "model.json"))
    for var in [v for v, _s, _p in st.PORT_TABLE] + ["COMPOSE_PROJECT_NAME", "ST_ALLOW"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(stack_checks, "_sleep", lambda _s: None)

    def install(containers: list[dict], **model: object) -> Path:
        (tmp_path / "model.json").write_text(json.dumps({"containers": containers, **model}))
        return log

    return install


def stacks() -> list[dict]:
    """The OTHER stack first (docker lists newest first), then a fresh `demo` stack."""
    live = "127.0.0.1:5174->8080/tcp"
    return [
        mk("opentranscribe-backend", "transcribe-app", "backend", live),
        mk("opentranscribe-postgres", "transcribe-app", "postgres", "127.0.0.1:5176->5432/tcp"),
        mk("otfresh-demo-backend", "otfresh-demo", "backend", "127.0.0.1:5374->8080/tcp"),
        mk("otfresh-demo-postgres", "otfresh-demo", "postgres", "127.0.0.1:5376->5432/tcp"),
    ]


def code_of(fn: Callable[[], object]) -> int:
    with pytest.raises(st.StackError) as err:
        fn()
    return int(err.value.code)


# ---------------------------------------------------------------- resolution


def test_mixed_exported_port_owned_by_another_project_is_refused(
    docker: Callable, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker(stacks())
    monkeypatch.setenv("POSTGRES_PORT", "5176")
    with pytest.raises(st.StackError) as err:
        st.resolve(repo, fresh="demo")
    assert err.value.code == st.WRONG_PROJECT
    assert "transcribe-app" in err.value.reason


def test_a_missing_offset_file_never_means_offset_zero(
    docker: Callable, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker(stacks())
    monkeypatch.setenv("BACKEND_PORT", "5174")  # the LIVE backend, exported by a stale shell
    assert code_of(lambda: st.resolve(repo, fresh="demo")) == st.WRONG_PROJECT
    monkeypatch.delenv("BACKEND_PORT")
    stack = st.resolve(repo, fresh="demo")
    assert stack.ports["BACKEND_PORT"] == 5374  # observed, never 5174 + a guessed offset


def test_fresh_resolves_its_own_project_and_observed_env_overridden_ports(
    docker: Callable, repo: Path
) -> None:
    docker(stacks())
    stack = st.resolve(repo, fresh="Demo")
    assert stack.project == "otfresh-demo"
    assert stack.ports == {"BACKEND_PORT": 5374, "POSTGRES_PORT": 5376}
    env = st.emit_env(stack)
    assert "export COMPOSE_PROJECT_NAME=otfresh-demo" in env
    assert "export E2E_BACKEND_URL=http://localhost:5374" in env


def test_stock_needs_exactly_one_live_project(docker: Callable, repo: Path) -> None:
    docker(stacks())
    assert st.resolve(repo, stock=True).project == "transcribe-app"
    docker(stacks() + [mk("x-pg", "opentranscribe", "postgres", "127.0.0.1:1->5432/tcp")])
    assert code_of(lambda: st.resolve(repo, stock=True)) == st.NO_PROJECT


def test_unknown_fresh_name_is_no_project(docker: Callable, repo: Path) -> None:
    docker(stacks())
    assert code_of(lambda: st.resolve(repo, fresh="nope")) == st.NO_PROJECT


def test_a_container_not_named_after_the_project_breaks_isolation(
    docker: Callable, repo: Path
) -> None:
    rows = stacks()
    rows.append(mk("opentranscribe-redis", "otfresh-demo", "redis"))
    docker(rows)
    assert code_of(lambda: st.resolve(repo, fresh="demo")) == st.WRONG_PROJECT


def test_docker_down_is_28_never_a_pass(docker: Callable, repo: Path) -> None:
    docker([], down=True)
    assert code_of(lambda: st.resolve(repo, fresh="demo")) == st.DOCKER


def test_docker_missing_is_28(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    assert code_of(lambda: st.resolve(repo, fresh="demo")) == st.DOCKER


# ---------------------------------------------------------------- checks


def demo_with(**pg: object) -> list[dict]:
    rows = stacks()
    rows[3].update(pg)
    return rows


def test_schema_behind_is_22_and_says_behind(docker: Callable, repo: Path) -> None:
    docker(demo_with(sql={"alembic_version": "v468_a"}))
    stack = st.resolve(repo, fresh="demo")
    with pytest.raises(st.StackError) as err:
        st.check_schema(stack)
    assert err.value.code == st.SCHEMA and "behind" in err.value.reason


def test_schema_foreign_is_22_and_at_head_is_clean(docker: Callable, repo: Path) -> None:
    docker(demo_with(sql={"alembic_version": "v999_zzz"}))
    with pytest.raises(st.StackError) as err:
        st.check_schema(st.resolve(repo, fresh="demo"))
    assert "ahead/foreign" in err.value.reason
    docker(demo_with(sql={"alembic_version": "v471_b"}))
    st.check_schema(st.resolve(repo, fresh="demo"))


def test_migrate_is_refused_outside_fresh_and_reltest(docker: Callable, repo: Path) -> None:
    docker(stacks())
    stack = st.resolve(repo, project="transcribe-app")
    assert code_of(lambda: st.check_schema(stack, migrate=True)) == st.WRONG_PROJECT


def mounted(source: str) -> dict:
    return {"Mounts": [{"Type": "bind", "Source": source, "Destination": "/app"}]}


def test_bind_of_another_checkout_is_23_and_this_checkout_is_clean(
    docker: Callable, repo: Path
) -> None:
    rows = stacks()
    rows[2]["inspect"] = mounted("/mnt/nvm/repos/transcribe-app-wt-cleanup-wavec/backend")
    docker(rows)
    stack = st.resolve(repo, fresh="demo")
    assert code_of(lambda: st.check_bind(stack, ["backend"])) == st.STALE_CODE
    rows[2]["inspect"] = mounted(str(repo / "backend"))
    docker(rows)
    st.check_bind(st.resolve(repo, fresh="demo"), ["backend"])


def head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, rel: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", rel],
        check=True,
    )


@pytest.mark.parametrize("value", ["abc1234-dirty", "unknown", ""])
def test_unattributable_image_sha_is_23(docker: Callable, repo: Path, value: str) -> None:
    rows = stacks()
    rows[2]["env"] = {"GIT_SHA": value}
    docker(rows)
    stack = st.resolve(repo, fresh="demo")
    with pytest.raises(st.StackError) as err:
        st.check_image(stack, ["backend"])
    assert err.value.code == st.STALE_CODE and "unattributable" in err.value.reason


def test_ancestor_image_with_backend_changed_since_is_23(docker: Callable, repo: Path) -> None:
    old = head_sha(repo)
    commit(repo, "backend/app/new.py")
    rows = stacks()
    rows[2]["env"] = {"GIT_SHA": old}
    docker(rows)
    assert code_of(lambda: st.check_image(st.resolve(repo, fresh="demo"), ["backend"])) == 23


@pytest.mark.parametrize("rel", ["scripts/y.sh", "backend/tests/unit/t.py"])
def test_ancestor_image_with_only_scripts_or_tests_changed_is_clean(
    docker: Callable, repo: Path, rel: str
) -> None:
    old = head_sha(repo)
    commit(repo, rel)
    rows = stacks()
    rows[2]["env"] = {"GIT_SHA": old}
    docker(rows)
    stack = st.resolve(repo, fresh="demo")
    assert st.run_checks(stack, ["code=image:backend"], {}) == []


def health(status: str, health_status: str = "", exit_code: int = 0, policy: str = "") -> dict:
    return {
        "State": {"Status": status, "Health": {"Status": health_status}, "ExitCode": exit_code},
        "HostConfig": {"RestartPolicy": {"Name": policy}},
    }


def test_unhealthy_is_24_and_starting_past_the_bound_is_24(
    docker: Callable, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = stacks()
    rows[3]["inspect"] = health("running", "unhealthy")
    docker(rows)
    stack = st.resolve(repo, fresh="demo")
    assert code_of(lambda: st.check_healthy(stack, ["postgres"])) == st.UNHEALTHY
    rows[3]["inspect"] = health("running", "starting")
    docker(rows)
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(stack_checks, "_now", lambda: float(next(clock)))
    assert code_of(lambda: st.check_healthy(stack, ["postgres"], timeout=1)) == st.UNHEALTHY


def test_healthy_and_a_finished_one_shot_are_clean(docker: Callable, repo: Path) -> None:
    rows = stacks()
    rows[3]["inspect"] = health("running", "healthy")
    rows.append(mk("otfresh-demo-migrate", "otfresh-demo", "migrate", state="exited"))
    rows[-1]["inspect"] = health("exited", "", 0, "no")
    docker(rows)
    stack = st.resolve(repo, fresh="demo")
    assert st.run_checks(stack, ["healthy:postgres,migrate"], {}) == []


def test_empty_preload_is_25(docker: Callable, repo: Path) -> None:
    docker(demo_with(sql={"shared_preload_libraries": ""}))
    stack = st.resolve(repo, fresh="demo")
    assert code_of(lambda: st.check_ext(stack, "pg_stat_statements")) == st.MISSING_SETTING


def test_loaded_extension_is_clean(docker: Callable, repo: Path) -> None:
    sql = {
        "shared_preload_libraries": "pg_stat_statements, auto_explain",
        "compute_query_id": "auto",
        "pg_extension": "1",
    }
    docker(demo_with(sql=sql))
    stack = st.resolve(repo, fresh="demo")
    assert st.run_checks(stack, ["ext:pg_stat_statements"], {}) == []


def test_cenv_mismatch_is_25(docker: Callable, repo: Path) -> None:
    rows = stacks()
    rows[2]["env"] = {"RATE_LIMIT_SEARCH_PER_MINUTE": "30"}
    docker(rows)
    stack = st.resolve(repo, fresh="demo")
    spec = "backend:RATE_LIMIT_SEARCH_PER_MINUTE=100000"
    assert code_of(lambda: st.check_cenv(stack, spec)) == st.MISSING_SETTING


def test_empty_fonts_dir_is_26_and_a_font_is_clean(docker: Callable, repo: Path) -> None:
    (repo / "frontend/static/fonts").mkdir(parents=True)
    docker(stacks())
    stack = st.resolve(repo, fresh="demo")
    assert code_of(lambda: st.check_asset(stack, "frontend-fonts")) == st.MISSING_ASSET
    (repo / "frontend/static/fonts/a.woff2").write_text("font")
    st.check_asset(stack, "frontend-fonts")


def test_env_var_unset_and_absent_from_dotenv_is_27(
    docker: Callable, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker(stacks())
    stack = st.resolve(repo, fresh="demo")
    monkeypatch.delenv("FLOWER_PASSWORD", raising=False)
    (repo / ".env").write_text("OTHER=1\n")
    assert code_of(lambda: st.check_env(stack, "FLOWER_PASSWORD")) == st.MISSING_ENV
    (repo / ".env").write_text("FLOWER_PASSWORD=x  # comment\n")
    st.check_env(stack, "FLOWER_PASSWORD")


def test_prestart_check_judges_env_and_asset_without_a_stack(
    docker: Callable, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker([])  # nothing running: a resolving check would be NO_PROJECT (20)
    monkeypatch.delenv("FLOWER_PASSWORD", raising=False)
    (repo / ".env").write_text("OTHER=1\n")
    argv = ["--repo-root", str(repo), "check", "--no-stack", "--no-banner"]
    assert st.main([*argv, "--require", "env:FLOWER_PASSWORD"]) == st.MISSING_ENV
    assert st.main([*argv, "--require", "asset:nginx-ssl"]) == st.MISSING_ASSET
    (repo / ".env").write_text("FLOWER_PASSWORD=x\n")
    (repo / "nginx/ssl").mkdir(parents=True)
    (repo / "nginx/ssl/server.crt").write_text("c")
    (repo / "nginx/ssl/server.key").write_text("k")
    assert st.main([*argv, "--require", "env:FLOWER_PASSWORD", "--require", "asset:nginx-ssl"]) == 0


# ---------------------------------------------------------------- prepare


@pytest.mark.parametrize("project", ["opentranscribe", "transcribe-app", "ot-reltest-lite"])
def test_prepare_refuses_live_and_reltest_without_touching_postgres(
    docker: Callable, repo: Path, project: str
) -> None:
    log = docker([mk("pg", project, "postgres", "127.0.0.1:5176->5432/tcp")])
    stack = st.resolve(repo, project=project)
    assert code_of(lambda: st.prepare_pgss(stack)) == st.WRONG_PROJECT
    assert "ALTER SYSTEM" not in log.read_text()


def test_prepare_cli_refuses_project_and_stock_with_usage(docker: Callable, repo: Path) -> None:
    docker(stacks())
    for flag in (["--project", "transcribe-app"], ["--stock"]):
        assert st.main(["--repo-root", str(repo), "prepare", *flag, "--pg-stat-statements"]) == 2


def test_prepare_when_already_loaded_restarts_nothing(docker: Callable, repo: Path) -> None:
    sql = {
        "shared_preload_libraries": "pg_stat_statements",
        "compute_query_id": "auto",
        "pg_extension": "1",
    }
    log = docker(demo_with(sql=sql))
    (repo / "opentr.sh").write_text('#!/bin/sh\necho "$@" >> "$FAKE_DOCKER_LOG"\n')
    (repo / "opentr.sh").chmod(0o755)
    st.prepare_pgss(st.resolve(repo, fresh="demo"))
    text = log.read_text()
    assert "restart-db" not in text and "ALTER SYSTEM" not in text


def test_prepare_runs_the_settings_and_restarts_through_opentr(
    docker: Callable, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = demo_with(sql={"ALTER SYSTEM": "", "shared_preload_libraries": ""})
    rows[3]["inspect"] = health("running", "healthy")
    rows[2]["inspect"] = health("running", "healthy")
    rows.append(mk("otfresh-demo-redis", "otfresh-demo", "redis"))
    rows[-1]["inspect"] = health("running", "healthy")
    log = docker(rows)
    (repo / "opentr.sh").write_text('#!/bin/sh\necho "OPENTR $@" >> "$FAKE_DOCKER_LOG"\n')
    (repo / "opentr.sh").chmod(0o755)
    monkeypatch.setattr(stack_checks, "http_status", lambda _u: 200)
    with pytest.raises(st.StackError):  # the fake never loads the library, so the re-check fails
        st.prepare_pgss(st.resolve(repo, fresh="demo"))
    text = log.read_text()
    assert text.count("ALTER SYSTEM") == len(st.PGSS_SETTINGS)
    assert "OPENTR restart-db --fresh demo" in text
    assert "OPENTR restart-backend --fresh demo" in text
    assert "shared_preload_libraries = pg_stat_statements, auto_explain" in text  # unquoted


# ---------------------------------------------------------------- allow, requirements, parity


def test_allow_overrides_with_a_recorded_line_and_never_bare(
    docker: Callable, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    docker(demo_with(sql={"alembic_version": "v468_a"}))
    stack = st.resolve(repo, fresh="demo")
    failures = st.run_checks(stack, ["schema-head"], {"schema-head": "known lag"})
    assert failures == [] and "OVERRIDDEN schema-head (known lag)" in capsys.readouterr().err
    assert len(st.run_checks(stack, ["schema-head"], {})) == 1
    with pytest.raises(st.StackError):
        st.parse_allow(["schema-head="], "")


def test_requirement_lists_keep_service_commas() -> None:
    got = st.split_requirements(
        ["schema-head,healthy:postgres,redis,backend", "asset:frontend-fonts"]
    )
    assert got == ["schema-head", "healthy:postgres,redis,backend", "asset:frontend-fonts"]


def test_every_failure_prints_one_attributable_line(
    docker: Callable, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    docker(demo_with(sql={"alembic_version": "v468_a"}))
    rc = st.main(["--repo-root", str(repo), "check", "--fresh", "demo", "--require", "schema-head"])
    err = capsys.readouterr().err
    assert rc == 22
    assert re.search(r"stack-target: FAIL SCHEMA\(22\) project=otfresh-demo: .*\. Fix: ", err)


def bash_sanitize(name: str) -> str:
    src = (REPO / "opentr.sh").read_text()
    body = re.search(r"fresh_sanitize_name\(\) \{\n(.*?)\n\}", src, re.DOTALL)
    assert body is not None
    out = subprocess.run(
        [
            "bash",
            "-c",
            f'fresh_sanitize_name() {{\n{body.group(1)}\n}}\nfresh_sanitize_name "$1"',
            "_",
            name,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@pytest.mark.parametrize(
    "name",
    ["demo", "Demo", "perf 1337", "a_b", "--x--", "", "ÅB", "p3gate", "rel-0.7.0", "A.B.C", "x  y",
     "UP", "9", "a-b-c", "a--b", "   ", "q/r", "t:1", "mut-session", "x!"],
)  # fmt: skip
def test_sanitize_is_byte_equal_to_the_bash_function(name: str) -> None:
    assert st.sanitize(name) == bash_sanitize(name)


def test_port_table_covers_every_port_var_opentr_declares() -> None:
    src = (REPO / "opentr.sh").read_text()
    declared = set(re.findall(r'^\s+"([A-Z_]+_PORT)=\d+"', src, re.MULTILINE))
    assert declared, "no FRESH_*_PORT_VARS entries parsed: the parity test would pass on nothing"
    missing = declared - {v for v, _s, _p in st.PORT_TABLE}
    assert not missing, (
        f"opentr.sh declares {sorted(missing)} but stack_target.PORT_TABLE has no row"
    )


def test_help_documents_every_exit_code() -> None:
    doc = st.__doc__ or ""
    for code in (0, 2, 20, 21, 22, 23, 24, 25, 26, 27, 28):
        assert re.search(rf"^\s+{code} ", doc, re.MULTILINE), f"exit code {code} undocumented"


def test_shell_compose_project_name_follows_an_exported_postgres_port(
    docker: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ports-only fresh target: the live project must not win just because it is allowlisted."""
    docker(stacks())
    monkeypatch.setenv("POSTGRES_PORT", "5376")
    out = subprocess.run(
        ["bash", "-c", f'source "{REPO}/scripts/lib/compose-project.sh"; compose_project_name'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == "otfresh-demo"


# ---------------------------------------------------------------- python API for scripts


def test_observed_port_prefers_the_exported_value_and_needs_no_docker(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")  # docker is unreachable
    assert st.observed_port("BACKEND_PORT", {"BACKEND_PORT": "6111"}) == "6111"


def test_observed_port_reads_the_resolved_project_not_the_stock_port(
    docker: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker(stacks())
    assert st.observed_port("BACKEND_PORT", {"COMPOSE_PROJECT_NAME": "otfresh-demo"}) == "5374"
    assert st.observed_container("postgres", {"COMPOSE_PROJECT_NAME": "otfresh-demo"}) == (
        "otfresh-demo-postgres"
    )


def test_observed_port_is_empty_when_nothing_resolves(
    docker: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker([], down=True)
    assert st.observed_port("BACKEND_PORT", {}) == ""
    assert st.observed_container("postgres", {}) == ""


def test_refuse_split_names_a_database_and_backend_of_two_projects(docker: Callable) -> None:
    docker(stacks())
    assert code_of(lambda: st.refuse_split({"POSTGRES_PORT": "5176", "BACKEND_PORT": "5374"})) == (
        st.WRONG_PROJECT
    )


def test_refuse_split_accepts_one_project_and_a_throwaway_port(docker: Callable) -> None:
    docker(stacks())
    assert st.refuse_split({"POSTGRES_PORT": "5376", "BACKEND_PORT": "5374"}) is None
    # nobody publishes 49321 (a throwaway container), so it names no project
    assert st.refuse_split({"POSTGRES_PORT": "49321", "BACKEND_PORT": "5174"}) is None
