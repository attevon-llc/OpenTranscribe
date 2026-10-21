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

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_REL = "scripts/e2e/update-visual-baselines.sh"
SCRIPT = REPO_ROOT / SCRIPT_REL
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "release" / "30-verify.sh"

FAKE_DOCKER = """\
#!/bin/bash
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
    return {"bin": bin_dir, "opentr": opentr, "volumes": volumes, "log": log}


def run_fn(
    call: str, sandbox: dict[str, Path], *, mode: str = "honour", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source the REAL script, point it at the fake opentr, run one snippet."""
    script = textwrap.dedent(f"""
        set -uo pipefail
        REPO_ROOT={REPO_ROOT!s}
        source "$REPO_ROOT/scripts/lib/compose-project.sh"
        source "$REPO_ROOT/{SCRIPT_REL}"
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


class TestTheSeedManifest:
    def test_it_is_derived_from_the_seeder_and_logged(self, tmp_path: Path, sandbox):
        media = tmp_path / "media"
        media.mkdir()
        (media / "seed_one.mp3").write_bytes(b"x" * 10)

        proc = run_fn(
            f'cd {REPO_ROOT}; derive_seed_manifest; printf "MANIFEST<%s>\\n" "$SEED_MANIFEST"',
            sandbox,
            env={"SEED_MEDIA_DIR": str(media)},
        )

        assert rc_of(proc) == 0, proc.stderr
        assert f"# source: {media}" in proc.stdout
        assert "seed_one.mp3\t10" in proc.stdout
        assert "Expected seed corpus: 1 file(s)" in proc.stderr

    def test_synthetic_fallback_is_refused_before_a_stack_is_started(self, tmp_path: Path, sandbox):
        empty = tmp_path / "empty"
        empty.mkdir()

        proc = run_fn(
            f"cd {tmp_path}; (derive_seed_manifest)", sandbox, env={"SEED_MEDIA_DIR": str(empty)}
        )

        assert rc_of(proc) == 3, proc.stdout
        assert "no real seed media" in proc.stderr


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


@pytest.mark.parametrize(
    "function",
    [
        "derive_seed_manifest",
        "assert_corpus_is_the_seed",
        "rebuild_capture_project",
        "destroy_capture_project",
    ],
)
def test_every_new_step_is_actually_invoked(function: str):
    """Definition is not invocation: an uncalled check looks exactly like a passing one."""
    source = SCRIPT.read_text(encoding="utf-8")
    definition = f"{function}() {{"
    assert definition in source, f"{function} is gone from {SCRIPT_REL}"
    body = source.split(definition, 1)[1]
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
