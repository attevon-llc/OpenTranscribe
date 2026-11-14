"""The capture wrapper's stack lifecycle must verify state, not trust exit codes (#1238).

``opentr.sh fresh-destroy`` is interactive: EOF at its y/N prompt (any backgrounded or piped
shell) was "cancelled" with exit 0. The wrapper logged only on a non-zero exit, so ``--destroy``
was a silent no-op outside a terminal that still reported success, and the issue's "destroy the
stack at the end" workaround only ever worked interactively. These tests drive the REAL
functions out of the real script (sourced) with a fake ``docker`` and a fake ``opentr.sh`` that
reproduce that exact behaviour, so a destroy that does not happen is observable as a failure.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.unit._demo_bundle import VERSION
from tests.unit._demo_bundle import item as demo_item
from tests.unit._demo_bundle import media_bytes
from tests.unit._demo_bundle import write_bundle

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_REL = "scripts/e2e/update-visual-baselines.sh"
SCRIPT = REPO_ROOT / SCRIPT_REL
CORPUS_LIB_REL = "scripts/lib/visual-seed-corpus.sh"
CORPUS_LIB = REPO_ROOT / CORPUS_LIB_REL
CORPUS_FILE = REPO_ROOT / "scripts" / "e2e" / "visual-seed-corpus.json"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "release" / "30-verify.sh"

FAKE_DOCKER = """\
#!/bin/bash
echo "$*" >> "${FAKE_DOCKER_LOG:-/dev/null}"
case "$1" in
  volume) cat "${FAKE_VOLUMES_FILE:?}" ;;
  info) echo "fake docker info" ;;
  *) exit 0 ;;
esac
"""

#: Reproduces the real tool: without --yes and without a tty the prompt read EOF, printed
#: "cancelled" and exited 0. With --yes it removes the project's volumes (mode "honour") or
#: still does nothing (mode "ignore", i.e. a destroy that silently failed to happen).
FAKE_OPENTR = """\
#!/bin/bash
echo "$*" >> "${FAKE_OPENTR_LOG:?}"
if [[ "$1" == "fresh-destroy" ]]; then
  yes=false
  for a in "$@"; do [[ "$a" == "--yes" ]] && yes=true; done
  if ! $yes; then echo "Destroy cancelled."; exit 0; fi
  if [[ "${FAKE_OPENTR_MODE:-honour}" == "honour" ]]; then
    : > "${FAKE_VOLUMES_FILE:?}"
  fi
fi
exit 0
"""

MANIFEST = (
    '# source: /home/someone/benchmark/test_audio\nseed_a.mp3\t14721443\nseed_"b".mp4\t18228379\n'
)


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    opentr = tmp_path / "opentr.sh"
    opentr.write_text(FAKE_OPENTR, encoding="utf-8")
    opentr.chmod(0o755)
    volumes = tmp_path / "volumes.txt"
    volumes.write_text("otfresh-visual_pgdata\notfresh-visual_models\n", encoding="utf-8")
    log = tmp_path / "opentr.log"
    log.write_text("", encoding="utf-8")
    docker_log = tmp_path / "docker.log"
    docker_log.write_text("", encoding="utf-8")
    return {
        "bin": bin_dir,
        "opentr": opentr,
        "volumes": volumes,
        "log": log,
        "docker_log": docker_log,
    }


def run_fn(
    call: str, sandbox: dict[str, Path], *, mode: str = "honour", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source the REAL script, point it at the fake opentr, run one snippet."""
    script = textwrap.dedent(f"""
        set -uo pipefail
        REPO_ROOT={REPO_ROOT!s}
        source "$REPO_ROOT/scripts/lib/compose-project.sh"
        source "$REPO_ROOT/{SCRIPT_REL}"
        source "$REPO_ROOT/scripts/lib/visual-baseline-provenance.sh"
        source "$REPO_ROOT/{CORPUS_LIB_REL}"
        set +e
        if [[ "$(type -t destroy_capture_project)" != "function" ]]; then
            echo "SCRIPT_NOT_SOURCED"; exit 97
        fi
        OPENTR={sandbox["opentr"]!s}
        FRESH_NAME=visual
        {call}
        echo "RC=$?"
    """)
    proc_env = {
        **os.environ,
        "PATH": f"{sandbox['bin']}{os.pathsep}{os.environ['PATH']}",
        "FAKE_VOLUMES_FILE": str(sandbox["volumes"]),
        "FAKE_OPENTR_LOG": str(sandbox["log"]),
        "FAKE_OPENTR_MODE": mode,
        "FAKE_DOCKER_LOG": str(sandbox["docker_log"]),
        **(env or {}),
    }
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=proc_env, timeout=120
    )


def rc_of(proc: subprocess.CompletedProcess[str]) -> int:
    assert "SCRIPT_NOT_SOURCED" not in proc.stdout, proc.stderr
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("RC="):
            return int(line.removeprefix("RC="))
    raise AssertionError(f"harness crashed before RC=\nstdout:\n{proc.stdout}\n{proc.stderr}")


def opentr_calls(sandbox: dict[str, Path]) -> list[str]:
    return sandbox["log"].read_text(encoding="utf-8").splitlines()


class TestDestroyIsVerifiedNotTrusted:
    def test_it_confirms_non_interactively_and_succeeds_when_the_volumes_are_gone(
        self, sandbox: dict[str, Path]
    ):
        proc = run_fn("destroy_capture_project", sandbox)

        assert rc_of(proc) == 0, proc.stderr
        assert opentr_calls(sandbox) == ["fresh-destroy visual --yes"]
        assert sandbox["volumes"].read_text(encoding="utf-8") == ""

    def test_a_destroy_that_did_not_happen_is_a_failure_even_though_opentr_exited_zero(
        self, sandbox: dict[str, Path]
    ):
        """The original defect: exit 0 from a destroy that removed nothing."""
        proc = run_fn("destroy_capture_project", sandbox, mode="ignore")

        assert rc_of(proc) == 1, proc.stdout
        assert "did not remove the volumes of otfresh-visual" in proc.stderr
        assert "otfresh-visual_pgdata" in proc.stderr

    def test_only_this_projects_volumes_count_and_the_prefix_is_anchored(
        self, sandbox: dict[str, Path]
    ):
        sandbox["volumes"].write_text(
            "otfresh-visual2_pgdata\notfresh-other_models\nopentranscribe_pgdata\n",
            encoding="utf-8",
        )

        proc = run_fn("destroy_capture_project", sandbox, mode="ignore")

        assert rc_of(proc) == 0, proc.stderr

    def test_stopping_with_destroy_flags_a_destroy_that_left_volumes(
        self, sandbox: dict[str, Path]
    ):
        proc = run_fn(
            "STACK_STARTED_BY_US=true; DESTROY_STACK=true; stop_capture_stack; "
            'echo "FAILED=$DESTROY_FAILED"',
            sandbox,
            mode="ignore",
        )

        assert "FAILED=true" in proc.stdout, proc.stdout
        assert "fresh-destroy visual --yes" in opentr_calls(sandbox)

    def test_stopping_without_destroy_keeps_the_volumes_and_never_destroys(
        self, sandbox: dict[str, Path]
    ):
        proc = run_fn(
            "STACK_STARTED_BY_US=true; DESTROY_STACK=false; stop_capture_stack; "
            'echo "FAILED=$DESTROY_FAILED"',
            sandbox,
        )

        assert "FAILED=false" in proc.stdout, proc.stdout
        assert opentr_calls(sandbox) == ["stop --fresh visual"]
        assert "otfresh-visual_pgdata" in sandbox["volumes"].read_text(encoding="utf-8")


class TestRebuild:
    def test_it_destroys_the_capture_project_before_anything_else(self, sandbox: dict[str, Path]):
        proc = run_fn("DRY_RUN=false; rebuild_capture_project", sandbox)

        assert rc_of(proc) == 0, proc.stderr
        assert opentr_calls(sandbox) == ["fresh-destroy visual --yes"]

    def test_a_dry_run_destroys_nothing(self, sandbox: dict[str, Path]):
        proc = run_fn("DRY_RUN=true; rebuild_capture_project", sandbox)

        assert rc_of(proc) == 0, proc.stderr
        assert opentr_calls(sandbox) == []
        assert "would destroy otfresh-visual" in proc.stderr

    def test_a_rebuild_that_cannot_remove_the_volumes_stops_the_run(self, sandbox: dict[str, Path]):
        proc = run_fn("DRY_RUN=false; (rebuild_capture_project)", sandbox, mode="ignore")

        assert rc_of(proc) == 3, proc.stdout
        assert "--rebuild could not remove" in proc.stderr

    def test_the_flag_parses_and_composes_with_keep(self, sandbox: dict[str, Path]):
        proc = run_fn(
            'parse_args --reason x --rebuild --keep; echo "REBUILD=$REBUILD_STACK KEEP=$KEEP_STACK"',
            sandbox,
        )

        assert "REBUILD=true KEEP=true" in proc.stdout, proc.stdout + proc.stderr


def _corpus(tmp_path: Path, items: list[str], bundle_version: str = VERSION) -> Path:
    path = tmp_path / "corpus.json"
    body = {
        "corpus_id": "neutral-test",
        "corpus_version": 3,
        "bundle_version": bundle_version,
        "items": items,
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


@pytest.fixture
def demo_bundle(tmp_path: Path) -> Path:
    items = [demo_item("a"), demo_item("b", ext="mp3"), demo_item("c")]
    return write_bundle(tmp_path / "bundle", items, {"ab": ["a", "b"], "bc": ["b", "c"]})


class TestTheSeedManifest:
    """The expected corpus is the bundle slice the corpus file names, validated in the bundle."""

    def test_it_is_derived_from_the_bundle_slice_and_logged(
        self, tmp_path: Path, sandbox, demo_bundle: Path
    ):
        env = {
            "DEMO_SEED_BUNDLE": str(demo_bundle),
            "VISUAL_SEED_CORPUS": str(_corpus(tmp_path, ["b", "a"])),
        }

        proc = run_fn(
            'derive_seed_manifest; printf "MANIFEST<%s>\\n" "$SEED_MANIFEST"', sandbox, env=env
        )

        assert rc_of(proc) == 0, proc.stderr
        assert (
            "MANIFEST<# source: neutral-test\n# corpus: neutral-test 3\n"
            f"# bundle_version: {VERSION}\n"
            f"b.mp3\t{len(media_bytes('b'))}\na.mp4\t{len(media_bytes('a'))}>"
        ) in proc.stdout
        assert "Expected seed corpus: 2 file(s), neutral-test v3" in proc.stderr

    def test_no_bundle_is_refused_before_a_stack_is_started(self, sandbox):
        proc = run_fn("(derive_seed_manifest)", sandbox, env={"DEMO_SEED_BUNDLE": ""})

        assert rc_of(proc) == 3, proc.stdout
        assert "DEMO_SEED_BUNDLE must name the demo seed bundle" in proc.stderr

    def test_a_bundle_of_another_version_is_refused(self, tmp_path: Path, sandbox, demo_bundle):
        env = {
            "DEMO_SEED_BUNDLE": str(demo_bundle),
            "VISUAL_SEED_CORPUS": str(_corpus(tmp_path, ["a"], bundle_version="2025.1.1")),
        }

        proc = run_fn("(derive_seed_manifest)", sandbox, env=env)

        assert rc_of(proc) == 3, proc.stdout
        assert f"the bundle is version {VERSION}; the selection pins 2025.1.1" in proc.stderr

    def test_an_item_the_bundle_lacks_is_refused(self, tmp_path: Path, sandbox, demo_bundle):
        env = {
            "DEMO_SEED_BUNDLE": str(demo_bundle),
            "VISUAL_SEED_CORPUS": str(_corpus(tmp_path, ["a", "zz"])),
        }

        proc = run_fn("(derive_seed_manifest)", sandbox, env=env)

        assert rc_of(proc) == 3, proc.stdout
        assert "zz: not in the bundle" in proc.stderr


def test_the_committed_corpus_is_a_small_fixed_slice_of_a_pinned_bundle():
    """3 to 5 named items (#43), a numeric version to bump, and the bundle version pinned."""
    corpus = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))

    assert 3 <= len(corpus["items"]) <= 5
    assert len(set(corpus["items"])) == len(corpus["items"])
    assert corpus["corpus_id"]
    assert isinstance(corpus["corpus_version"], int)
    assert corpus["bundle_version"]


class TestTheCorpusIsImportedIntoTheCaptureStack:
    """`seed_capture_corpus`: the slice goes into the stack's backend, imported only if empty."""

    def _run(self, tmp_path: Path, sandbox, demo_bundle: Path, container: str, dry: str = "false"):
        env = {
            "DEMO_SEED_BUNDLE": str(demo_bundle),
            "VISUAL_SEED_CORPUS": str(_corpus(tmp_path, ["a", "b"])),
        }
        call = (
            f'derive_seed_manifest; st_container() {{ echo "{container}"; }}; '
            f'WORKDIR="{tmp_path}/run-1"; DRY_RUN={dry}; seed_capture_corpus'
        )
        return run_fn(call, sandbox, env=env)

    def test_the_slice_is_copied_in_and_imported_into_the_admin_only_if_empty(
        self, tmp_path: Path, sandbox, demo_bundle: Path
    ):
        proc = self._run(tmp_path, sandbox, demo_bundle, "otfresh-visual-backend-1")

        assert rc_of(proc) == 0, proc.stderr
        calls = sandbox["docker_log"].read_text(encoding="utf-8").splitlines()
        assert calls == [
            f"cp {tmp_path}/run-1/seed-bundle otfresh-visual-backend-1:/tmp/ot-visual-seed-run-1",
            "exec -e DEMO_MODE=false -e DEMO_USER_EMAIL=admin@example.com "
            "-e DEMO_USER_PASSWORD=password otfresh-visual-backend-1 python -m "
            "app.scripts.demo_seed import --bundle /tmp/ot-visual-seed-run-1 --if-empty",
        ]
        slice_manifest = json.loads(
            (tmp_path / "run-1" / "seed-bundle" / "manifest.json").read_text(encoding="utf-8")
        )
        assert [i["id"] for i in slice_manifest["items"]] == ["a", "b"]
        assert [c["id"] for c in slice_manifest["conversations"]] == ["ab"]

    def test_no_backend_container_is_a_failure_and_nothing_is_run(
        self, tmp_path: Path, sandbox, demo_bundle: Path
    ):
        proc = self._run(tmp_path, sandbox, demo_bundle, "")

        assert rc_of(proc) == 1, proc.stdout
        assert "no running backend container" in proc.stderr
        assert sandbox["docker_log"].read_text(encoding="utf-8") == ""

    def test_a_dry_run_imports_nothing(self, tmp_path: Path, sandbox, demo_bundle: Path):
        proc = self._run(tmp_path, sandbox, demo_bundle, "x", dry="true")

        assert rc_of(proc) == 0, proc.stderr
        assert sandbox["docker_log"].read_text(encoding="utf-8") == ""
        assert not (tmp_path / "run-1").exists()


def test_the_capture_stack_is_not_started_with_the_benchmark_seed(sandbox):
    """#43: the benchmark recordings never reach a baseline; the corpus is imported instead."""
    proc = run_fn(
        "BASE_FRONTEND_PORT=1; BASE_BACKEND_PORT=2; BASE_MOCK_LLM_PORT=3; BASE_POSTGRES_PORT=4; "
        "BASE_MINIO_PORT=5; BASE_OPENSEARCH_PORT=6; DRY_RUN=true; PORT_OFFSET=100; "
        "start_capture_stack",
        sandbox,
    )

    assert rc_of(proc) == 0, proc.stderr
    assert "start dev --fresh visual --port-offset 100 --with-mock-llm" in proc.stderr
    assert "--seed-benchmark" not in proc.stderr


class TestTheSidecarRecordsTheCorpus:
    def test_the_corpus_is_in_the_sidecar_and_the_old_fields_survive(self, tmp_path: Path, sandbox):
        shots, work, out = tmp_path / "shots", tmp_path / "work", tmp_path / "out"
        for d in (shots, work, out):
            d.mkdir()
        (shots / "gallery-light.png").write_bytes(b"")
        (work / "report.tsv").write_text("gallery-light\tCHANGED\t6.8000%\n", encoding="utf-8")

        proc = run_fn(
            f'SCREENSHOT_DIR="{shots}"; PROVENANCE_DIR="{out}"; WORKDIR="{work}"; '
            f"SELECTED_SURFACES=(gallery); REASON=x; PORT_OFFSET=100; GPU_DEVICE=''; "
            f"SEED_MANIFEST={shlex.quote(MANIFEST)}; resolve_git_provenance; record_provenance",
            sandbox,
        )
        assert rc_of(proc) == 0, proc.stderr

        sidecar = json.loads((out / "gallery-light.json").read_text(encoding="utf-8"))
        assert sidecar["stack"]["seeded"] is True
        assert sidecar["corpus"]["files"] == [
            {"filename": "seed_a.mp3", "size": 14721443},
            {"filename": 'seed_"b".mp4', "size": 18228379},
        ]
        assert sidecar["corpus"]["library_total"] == 2
        # A committed sidecar must not carry the photographer's home directory.
        assert sidecar["corpus"]["seed_source"] == "test_audio"
        assert sidecar["corpus"]["corpus_id"] is None
        assert sidecar["corpus"]["bundle_version"] is None

    def test_the_demo_corpus_id_version_and_bundle_are_recorded(self, tmp_path: Path, sandbox):
        shots, work, out = tmp_path / "shots", tmp_path / "work", tmp_path / "out"
        for d in (shots, work, out):
            d.mkdir()
        (shots / "gallery-dark.png").write_bytes(b"")
        manifest = (
            "# source: neutral-visual\n# corpus: neutral-visual 1\n"
            "# bundle_version: 2026.11.1\nfema-emergency-kit.mp4\t8391745\n"
        )

        proc = run_fn(
            f'SCREENSHOT_DIR="{shots}"; PROVENANCE_DIR="{out}"; WORKDIR="{work}"; '
            f"SELECTED_SURFACES=(gallery); REASON=x; PORT_OFFSET=100; GPU_DEVICE=''; "
            f"SEED_MANIFEST={shlex.quote(manifest)}; resolve_git_provenance; record_provenance",
            sandbox,
        )
        assert rc_of(proc) == 0, proc.stderr

        corpus = json.loads((out / "gallery-dark.json").read_text(encoding="utf-8"))["corpus"]
        assert corpus == {
            "seed_source": "neutral-visual",
            "corpus_id": "neutral-visual",
            "corpus_version": 1,
            "bundle_version": "2026.11.1",
            "files": [{"filename": "fema-emergency-kit.mp4", "size": 8391745}],
            "library_total": 1,
        }


@pytest.mark.parametrize(
    ("function", "defined_in"),
    [
        ("derive_seed_manifest", CORPUS_LIB),
        ("seed_capture_corpus", CORPUS_LIB),
        ("assert_corpus_is_the_seed", SCRIPT),
        ("rebuild_capture_project", SCRIPT),
        ("destroy_capture_project", SCRIPT),
    ],
)
def test_every_new_step_is_actually_invoked(function: str, defined_in: Path):
    """Definition is not invocation: an uncalled check looks exactly like a passing one."""
    definition = f"{function}() {{"
    assert definition in defined_in.read_text(encoding="utf-8"), f"{function} is gone"
    source = SCRIPT.read_text(encoding="utf-8")
    # A step defined in the script is called after its definition; a library's, anywhere.
    body = source.split(definition, 1)[1] if defined_in == SCRIPT else source
    call_sites = [
        line
        for line in body.splitlines()
        if function in line and not line.lstrip().startswith("#") and definition not in line
    ]
    assert call_sites, f"{function} is defined in {SCRIPT_REL} but never called"


def test_the_stale_baseline_hint_names_the_wrapper_not_a_hand_written_incantation():
    """The raw `start --seed-benchmark` + UPDATE_SCREENSHOTS hint skipped every safety (#1238)."""
    text = VERIFY_SCRIPT.read_text(encoding="utf-8")
    line = next(entry for entry in text.splitlines() if "frontend/src has" in entry)
    index = text.splitlines().index(line)
    hint = text.splitlines()[index + 1]

    assert "scripts/e2e/update-visual-baselines.sh" in hint
    assert "UPDATE_SCREENSHOTS" not in hint
    assert "--seed-benchmark" not in hint
