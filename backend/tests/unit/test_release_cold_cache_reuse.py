"""R7: Scenario D can run early (`65-rehearse.sh --cold-cache-only`) and a later full
`rehearse` reuses that evidence only when it is still valid; plus the pre-publish
`nginx -t` check of the frontend/docs arm64 legs.

Drives the REAL 65-rehearse.sh in the scratch tree of test_release_cold_model_download.py with a
fake docker. Nothing builds an image; the cold scenario is a stub that writes oracle evidence.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_release_cold_model_download import _WRITE_EVIDENCE
from tests.unit.test_release_cold_model_download import REHEARSE_SH
from tests.unit.test_release_cold_model_download import _scratch

pytestmark = pytest.mark.unit

PASS_EVIDENCE = _WRITE_EVIDENCE.format(pyannote_rc=0, rc=0)
VERSION = "v9.9.9"


def _docker(repo: Path, image_id: str, log: Path) -> None:
    (repo.parent / "bin" / "docker").write_text(
        '#!/bin/bash\necho "$*" >> "' + str(log) + '"\n'
        f'if [[ "$1 $2" == "image inspect" ]]; then echo "{image_id}"; fi\nexit 0\n'
    )


def _invoke(repo: Path, args: list[str], env_extra: dict[str, str]):
    env = {
        "PATH": f"{repo.parent / 'bin'}:/usr/bin:/bin",
        "RELEASE_VERSION": VERSION,
        "JSON_OUT": "true",
        "TO_BRANCH": "master",
        **env_extra,
    }
    return subprocess.run(
        [str(repo / "scripts/release/65-rehearse.sh"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _criteria(proc) -> dict:
    return {c["id"]: c for c in json.loads(proc.stdout.strip().splitlines()[-1])["criteria"]}


def _cold_only(repo: Path, tmp_path: Path):
    return _invoke(
        repo, ["--cold-cache-only", VERSION], {"COLD_CACHE_ROOT": str(tmp_path / "early")}
    )


def test_cold_only_runs_d_alone_records_root_and_image_and_keeps_the_cache(tmp_path: Path):
    repo, markers = _scratch(tmp_path, PASS_EVIDENCE)
    _docker(repo, "sha256:abc", tmp_path / "docker.log")
    proc = _cold_only(repo, tmp_path)
    assert proc.returncode == 0, proc.stderr
    state = repo / ".release" / VERSION
    assert (state / "cold-cache-root").read_text().strip() == str(tmp_path / "early")
    assert (state / "cold-cache-image-id").read_text().strip() == "sha256:abc"
    assert not (markers / "fresh.log").exists(), "cold-only must not run Scenario A"
    docker_calls = (tmp_path / "docker.log").read_text()
    assert "stop" not in docker_calls and " rm" not in docker_calls


def test_cold_only_failure_leaves_no_reusable_evidence(tmp_path: Path):
    repo, _ = _scratch(tmp_path, _WRITE_EVIDENCE.format(pyannote_rc=1, rc=1))
    _docker(repo, "sha256:abc", tmp_path / "docker.log")
    proc = _cold_only(repo, tmp_path)
    assert proc.returncode == 1
    assert not (repo / ".release" / VERSION / "cold-cache-root").exists()


def test_full_rehearse_reuses_valid_early_evidence_without_rerunning_d(tmp_path: Path):
    repo, markers = _scratch(tmp_path, PASS_EVIDENCE)
    _docker(repo, "sha256:abc", tmp_path / "docker.log")
    assert _cold_only(repo, tmp_path).returncode == 0
    ran_once = (markers / "cold.log").read_text()
    proc = _invoke(repo, [VERSION], {})
    assert proc.returncode == 0, proc.stderr
    assert (markers / "cold.log").read_text() == ran_once, "D must not run a second time"
    crit = _criteria(proc)
    for cid in (
        "cold-cache-model-download",
        "cold-cache-weights-present",
        "cold-cache-nltk-hardlinks-fail-loudly",
    ):
        assert crit[cid]["outcome"] == "pass", crit[cid]
    assert f"seed={tmp_path / 'early' / 'cache'} " in (markers / "fresh.log").read_text()


def test_a_different_image_id_makes_rehearse_rerun_d(tmp_path: Path):
    """The stale-root case: the image was rebuilt after D ran, so D proved nothing about it."""
    repo, markers = _scratch(tmp_path, PASS_EVIDENCE)
    _docker(repo, "sha256:abc", tmp_path / "docker.log")
    assert _cold_only(repo, tmp_path).returncode == 0
    _docker(repo, "sha256:REBUILT", tmp_path / "docker.log")
    proc = _invoke(repo, [VERSION], {})
    assert proc.returncode == 0, proc.stderr
    assert (markers / "cold.log").read_text().count("ran") == 2, "D must re-run on a changed image"


def test_an_unreadable_image_id_never_matches(tmp_path: Path):
    repo, markers = _scratch(tmp_path, PASS_EVIDENCE)
    _docker(repo, "sha256:abc", tmp_path / "docker.log")
    assert _cold_only(repo, tmp_path).returncode == 0
    _docker(repo, "", tmp_path / "docker.log")
    _invoke(repo, [VERSION], {})
    assert (markers / "cold.log").read_text().count("ran") == 2


def test_early_evidence_that_no_longer_passes_the_oracle_is_not_reused(tmp_path: Path):
    repo, markers = _scratch(tmp_path, PASS_EVIDENCE)
    _docker(repo, "sha256:abc", tmp_path / "docker.log")
    assert _cold_only(repo, tmp_path).returncode == 0
    (tmp_path / "early" / "groups.tsv").write_text("whisperx\t0\t1\npyannote\t1\t1\n")
    _invoke(repo, [VERSION], {})
    assert (markers / "cold.log").read_text().count("ran") == 2


def _arm64_fn() -> str:
    lines = REHEARSE_SH.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("arm64_web_legs_outcome() {"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _arm64(tmp_path: Path, legs: str | None, docker_body: str, patch: str = "", host_only: int = 0):
    root = tmp_path / "r"
    (root / ".release" / VERSION).mkdir(parents=True)
    if legs is not None:
        (root / ".release" / VERSION / "legs.tsv").write_text(legs)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "d.log"
    (bindir / "docker").write_text(f'#!/bin/bash\necho "$*" >> {log}\n{docker_body}\n')
    (bindir / "docker").chmod(0o755)
    script = f'set -uo pipefail\nREPO_ROOT="{root}"; VERSION={VERSION}; PATCH_SKIP_REASON="{patch}"; HOST_ONLY={host_only}\n{_arm64_fn()}\narm64_web_legs_outcome'
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
    ).stdout.strip()
    log.touch()
    return out, log.read_text()


LEGS = (
    "frontend\tdavidamacey/opentranscribe-frontend\tlinux/arm64\tfe:v9.9.9-web-arm64\tsha256:1\n"
    "docs\tdavidamacey/opentranscribe-docs\tlinux/arm64\tdocs:v9.9.9-web-arm64\tsha256:2\n"
    "frontend\tdavidamacey/opentranscribe-frontend\tlinux/amd64\tfe:v9.9.9-web-amd64\tsha256:3\n"
    "backend-lite\tx\tlinux/arm64\tlite:v9.9.9-cpu-arm64\tsha256:4\n"
)


def test_arm64_web_legs_run_nginx_t_on_the_remote_context_for_frontend_and_docs_only(tmp_path):
    out, calls = _arm64(tmp_path, LEGS, "exit 0")
    assert out.startswith("pass\t2 arm64 web leg(s)"), out
    assert "--context remote-arm64 run --rm fe:v9.9.9-web-arm64 nginx -t" in calls
    assert "--context remote-arm64 run --rm docs:v9.9.9-web-arm64 nginx -t" in calls
    assert (
        "web-amd64" not in calls.replace("save", "")
        or "run" not in calls.split("web-amd64")[0][-40:]
    )
    assert "lite:" not in calls, "only frontend and docs legs are probed here"


def test_a_failing_nginx_t_is_a_fail_naming_the_leg(tmp_path):
    out, _ = _arm64(tmp_path, LEGS, 'if [[ "$*" == *"nginx -t"* ]]; then exit 1; fi; exit 0')
    assert out.startswith("fail\t") and "fe:v9.9.9-web-arm64: nginx -t failed" in out, out


def test_a_missing_context_or_legs_file_is_not_measured_never_a_pass(tmp_path):
    out, _ = _arm64(
        tmp_path, LEGS, 'if [[ "$1 $2" == "context inspect" ]]; then exit 1; fi; exit 0'
    )
    assert out.startswith("not-measured\t") and "docker context" in out
    out2, _ = _arm64(tmp_path / "b", None, "exit 0")
    assert out2.startswith("not-measured\t")


def test_a_host_arch_only_build_is_not_measured_and_never_touches_a_remote_daemon(tmp_path):
    out, calls = _arm64(tmp_path, LEGS, "exit 0", host_only=1)
    assert out.startswith("not-measured\thost-arch-only build")
    assert calls == ""


def test_a_patch_waiver_is_not_measured(tmp_path):
    out, calls = _arm64(tmp_path, LEGS, "exit 0", patch="no triggers")
    assert out.startswith("not-measured\tno triggers")
    assert calls == ""


def test_arm64_web_legs_criterion_is_blocking_so_an_unreachable_host_cannot_publish():
    import yaml

    from tests.unit.test_release_cold_model_download import CRITERIA_YAML

    doc = yaml.safe_load(CRITERIA_YAML.read_text())
    sev = {c["id"]: c["severity"] for c in doc["stages"]["rehearse"]["criteria"]}
    assert sev["arm64-web-legs-run"] == "blocking"


def test_unreachable_arm64_host_fails_the_stage_with_a_clear_message(tmp_path: Path):
    """Real rehearse, no legs.tsv: not-measured on a blocking criterion fails; D passes."""
    repo, _ = _scratch(tmp_path, PASS_EVIDENCE)
    _docker(repo, "sha256:abc", tmp_path / "docker.log")
    proc = _invoke(repo, [VERSION], {"COLD_CACHE_ROOT": str(tmp_path / "c")})
    crit = _criteria(proc)["arm64-web-legs-run"]
    assert crit["outcome"] == "not-measured" and crit["status"] == "fail"
    assert "legs.tsv" in crit["detail"] and "--force-rehearse" in crit["fix"]
