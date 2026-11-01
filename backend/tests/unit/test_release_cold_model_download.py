"""Rehearsal Scenario D — the model downloader from a cold cache — must be able to FAIL.

THE BUG IT EXISTS FOR

v0.6.0 shipped `scripts/download-models.py` with a `pyannote` group that exits 1 from an
empty cache on every host, CPU and GPU (measured 2026-11-01 on the published image:
`IndexError: list index out of range` from transformers/pipelines/base.py, reached through
whisperx transcribing synthetic sine-wave audio that VAD rejects). Every release gate passed:
the rehearsal installs unattended, which skips the download, then seeds a warm cache.

WHAT IS TESTED HERE (no docker, no network)

* the oracle, `scripts/release-tests/lib/cold-model-download.py`, driven as bash drives it
  (subprocess): group derivation, the per-group artifact check against real directory trees
  (hardlinks, dangling hub symlinks, zero-byte weights), and the verdict mapping, including
  the exact `groups.tsv` the v0.6.0 image produced;
* that the scenario's container mounts equal `scripts/download-models.sh`'s, and that it can
  only run CPU-only, against the versioned image, with the token passed by name;
* the REAL `65-rehearse.sh` in a scratch tree: Scenario D's evidence decides its three
  criteria AND the stage's exit code, and only a passing D hands its cache to Scenario A.

The live leg (the real image, real downloads) is the scenario itself; its red and green runs
are recorded in docs/design/v060_release_runbook.md.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
ORACLE = REPO_ROOT / "scripts" / "release-tests" / "lib" / "cold-model-download.py"
SCENARIO = REPO_ROOT / "scripts" / "release-tests" / "test-cold-model-download.sh"
DOWNLOADER_PY = REPO_ROOT / "scripts" / "download-models.py"
DOWNLOADER_SH = REPO_ROOT / "scripts" / "download-models.sh"
REHEARSE_SH = REPO_ROOT / "scripts" / "release" / "65-rehearse.sh"
CRITERIA_LIB_SH = REPO_ROOT / "scripts" / "release" / "criteria-lib.sh"
CRITERIA_YAML = REPO_ROOT / "scripts" / "release" / "release-criteria.yaml"

WHISPER_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
COLD_CRITERIA = (
    "cold-cache-model-download",
    "cold-cache-weights-present",
    "cold-cache-nltk-hardlinks-fail-loudly",
)

#: The groups.tsv the v0.6.0 image + v0.6.0 downloader wrote from an empty cache on CPU
#: (2026-11-01, ~/ot-cleanup-work/coldcache). Only the rc column matters to the oracle.
V060_GROUPS_TSV = (
    "whisperx\t0\t53\npyannote\t1\t47\nnltk\t0\t36\nsentence-transformers\t0\t49\n"
    "reranker\t0\t40\nspeaker-attributes\t0\t45\nopensearch\t0\t41\nredaction\t0\t70\n"
    "diar-native\t0\t120\n"
)


def _oracle_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cold_model_download", ORACLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _oracle(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ORACLE), *args], capture_output=True, text=True, timeout=60
    )


def _downloader_groups() -> list[str]:
    proc = _oracle("groups", str(DOWNLOADER_PY))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split()


# ─── group derivation ───────────────────────────────────────────────────────


def test_groups_are_read_from_the_downloaders_own_table() -> None:
    groups = _downloader_groups()
    assert "pyannote" in groups, groups
    assert len(groups) == len(set(groups))
    oracle = _oracle_module()
    assert set(groups) == set(oracle.KNOWN_GROUPS), (
        "download-models.py's DOWNLOAD_GROUPS and the oracle's expectations disagree: "
        f"no expectation for {sorted(set(groups) - oracle.KNOWN_GROUPS)}, stale expectation "
        f"for {sorted(oracle.KNOWN_GROUPS - set(groups))}. Add the group's weights to "
        "cold-model-download.py's check_group in the same commit as the group."
    )


@pytest.mark.parametrize(
    "source",
    ["DOWNLOAD_GROUPS = build_groups()\n", "OTHER = {'a': f}\n", "DOWNLOAD_GROUPS = {k: v}\n"],
)
def test_groups_reader_refuses_what_it_cannot_read_literally(tmp_path: Path, source: str) -> None:
    script = tmp_path / "download-models.py"
    script.write_text(source)
    proc = _oracle("groups", str(script))
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout == ""


def test_every_model_the_oracle_names_is_named_by_the_downloader_and_its_consumer() -> None:
    """The oracle is a second opinion, so it must not invent model ids — nor keep a stale one."""
    oracle = _oracle_module()
    downloader = DOWNLOADER_PY.read_text()
    app_text = "\n".join(p.read_text() for p in (REPO_ROOT / "backend" / "app").rglob("*.py"))
    names = [repo_id for repos in oracle.HF_REPOS.values() for repo_id, _, _ in repos]
    assert len(names) >= 6
    not_downloaded = [n for n in names if n not in downloader]
    not_consumed = [n for n in names if n not in app_text]
    assert not not_downloaded, (
        f"the oracle expects models the downloader never fetches: {not_downloaded}"
    )
    assert not not_consumed, f"the oracle expects models no backend code loads: {not_consumed}"


# ─── artifact verification against real trees ──────────────────────────────


def _hf_repo(cache: Path, repo_id: str, root: str, files: tuple[str, ...]) -> Path:
    """A hub-layout repo: real bytes in blobs/, the named files as symlinks in a snapshot."""
    repo = cache / root / ("models--" + repo_id.replace("/", "--"))
    snap = repo / "snapshots" / "rev1"
    for n, name in enumerate(files):
        blob = repo / "blobs" / f"blob{n}"
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"w" * 64)
        link = snap / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(os.path.relpath(blob, link.parent))
    return repo


def _complete_cache(root: Path) -> Path:
    oracle = _oracle_module()
    cache = root / "cache"
    _hf_repo(cache, WHISPER_REPO, oracle.HUB, oracle.WHISPER_REQUIRED)
    for repos in oracle.HF_REPOS.values():
        for repo_id, repo_root, required in repos:
            files = tuple("model.safetensors" if r == oracle.ANY_WEIGHT else r for r in required)
            _hf_repo(cache, repo_id, repo_root, files)
    for res in oracle.NLTK_RESOURCES:
        d = cache / "nltk_data" / res
        d.mkdir(parents=True)
        (d / "data.txt").write_text("x")
    os_model = cache / "opensearch-ml" / "all-MiniLM-L6-v2"
    os_model.mkdir(parents=True)
    (os_model / "all-MiniLM-L6-v2.zip").write_bytes(b"w")
    (os_model / "config.json").write_text("{}")
    dn = cache / "diar-native"
    dn.mkdir(parents=True)
    (dn / "segmentation.onnx").write_bytes(b"w")
    return cache


def _verify(cache: Path, groups: list[str], whisper_repo: str = WHISPER_REPO) -> tuple[int, dict]:
    proc = _oracle(
        "verify",
        "--cache",
        str(cache),
        "--groups",
        ",".join(groups),
        "--whisper-repo",
        whisper_repo,
    )
    rows = {}
    for line in proc.stdout.splitlines():
        group, verdict, detail = line.split("\t")
        rows[group] = (verdict, detail)
    return proc.returncode, rows


def test_a_complete_cache_passes_every_group(tmp_path: Path) -> None:
    groups = _downloader_groups()
    rc, rows = _verify(_complete_cache(tmp_path), groups)
    assert rc == 0, rows
    assert set(rows) == set(groups)
    assert {v for v, _ in rows.values()} == {"pass"}


PYANNOTE = "huggingface/hub/models--pyannote--speaker-diarization-community-1/snapshots/rev1"


def _drop_pyannote_blob(cache: Path) -> None:
    # A dangling hub symlink is what an interrupted download leaves behind.
    repo = cache / "huggingface/hub/models--pyannote--speaker-diarization-community-1"
    shutil.rmtree(repo / "blobs")


def _pyannote_config_only(cache: Path) -> None:
    # The pipeline config arrived, its sub-model weights did not: ANY-weight logic would pass.
    for sub in ("segmentation", "embedding", "plda"):
        shutil.rmtree(cache / PYANNOTE / sub)


def _reranker_in_the_wrong_root(cache: Path) -> None:
    # The app reads the reranker from the sentence-transformers home, nowhere else.
    st = cache / "sentence-transformers/models--cross-encoder--ms-marco-MiniLM-L-6-v2"
    shutil.move(st, cache / "huggingface/hub" / st.name)


def _missing_shard(cache: Path) -> None:
    snap = cache / "huggingface/hub/models--unitary--toxic-bert/snapshots/rev1"
    (snap / "model.safetensors.index.json").write_text(
        '{"weight_map": {"a": "model-00001-of-00002.safetensors", '
        '"b": "model-00002-of-00002.safetensors"}}'
    )
    (snap / "model-00001-of-00002.safetensors").write_bytes(b"w")


def _redaction_partial(cache: Path) -> None:
    # download_redaction_models returns 'partial' (exit 0) when one model fails.
    shutil.rmtree(cache / "huggingface/hub/models--unitary--multilingual-toxic-xlm-roberta")


def _hardlink_nltk(cache: Path) -> None:
    src = cache / "nltk_data/corpora/stopwords/data.txt"
    os.link(src, cache / "nltk_data/corpora/stopwords/linked.txt")


def _empty_speaker_weights(cache: Path) -> None:
    repo = cache / "huggingface/hub/models--prithivMLmods--Common-Voice-Gender-Detection"
    for blob in (repo / "blobs").iterdir():
        blob.write_bytes(b"")


DAMAGE: dict[str, tuple[str, Callable[[Path], None], str]] = {
    "dangling-hub-symlink": ("pyannote", _drop_pyannote_blob, "incomplete snapshot"),
    "pyannote-config-only": ("pyannote", _pyannote_config_only, "segmentation/pytorch_model.bin"),
    "reranker-wrong-root": (
        "reranker",
        _reranker_in_the_wrong_root,
        "not in sentence-transformers",
    ),
    "missing-shard": ("redaction", _missing_shard, "model-00002-of-00002.safetensors"),
    "redaction-partial": ("redaction", _redaction_partial, "multilingual-toxic-xlm-roberta"),
    "nltk-hardlinked": ("nltk", _hardlink_nltk, "multiply-linked"),
    "nltk-missing": (
        "nltk",
        lambda c: shutil.rmtree(c / "nltk_data/tokenizers/punkt_tab"),
        "punkt_tab missing",
    ),
    "zero-byte-weights": ("speaker-attributes", _empty_speaker_weights, "non-empty weight"),
    "sentence-transformers-empty": (
        "sentence-transformers",
        lambda c: shutil.rmtree(
            c / "sentence-transformers/models--sentence-transformers--all-MiniLM-L6-v2"
        ),
        "not in sentence-transformers",
    ),
    "opensearch-no-config": (
        "opensearch",
        lambda c: (c / "opensearch-ml/all-MiniLM-L6-v2/config.json").unlink(),
        "config.json missing",
    ),
    "diar-native-no-onnx": (
        "diar-native",
        lambda c: (c / "diar-native/segmentation.onnx").unlink(),
        "no .onnx",
    ),
    "whisper-not-downloaded": (
        "whisperx",
        lambda c: shutil.rmtree(
            c / "huggingface/hub/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
        ),
        "not in huggingface/hub",
    ),
}


@pytest.mark.parametrize("case", sorted(DAMAGE))
def test_each_kind_of_damage_fails_exactly_its_own_group(tmp_path: Path, case: str) -> None:
    group, damage, needle = DAMAGE[case]
    cache = _complete_cache(tmp_path)
    damage(cache)
    rc, rows = _verify(cache, _downloader_groups())
    assert rc == 1, rows
    assert rows[group][0] == "fail", rows[group]
    assert needle in rows[group][1], rows[group]
    others = {g: v for g, (v, _) in rows.items() if g != group}
    assert set(others.values()) == {"pass"}, f"damage to {group} leaked into {others}"


def test_an_unresolved_whisper_repo_is_a_failure_not_a_skip(tmp_path: Path) -> None:
    rc, rows = _verify(_complete_cache(tmp_path), ["whisperx"], whisper_repo="")
    assert rc == 1
    assert rows["whisperx"][0] == "fail" and "could not check" in rows["whisperx"][1]


def test_verify_cannot_check_a_missing_cache_or_an_unknown_group(tmp_path: Path) -> None:
    assert _oracle("verify", "--cache", str(tmp_path / "nope"), "--groups", "nltk").returncode == 3
    rc, rows = _verify(_complete_cache(tmp_path), ["nltk", "brand-new-group"])
    assert rc == 3
    assert rows["brand-new-group"][0] == "fail"


# ─── verdict mapping ────────────────────────────────────────────────────────


def _evidence(root: Path, *, groups_tsv: str | None, provenance: str = "verified img") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    expected = [line.split("\t")[0] for line in V060_GROUPS_TSV.splitlines()]
    (root / "groups-expected.txt").write_text("\n".join(expected) + "\n")
    (root / "provenance.txt").write_text(provenance + "\n")
    if groups_tsv is not None:
        (root / "groups.tsv").write_text(groups_tsv)
    return root


def _outcome(root: Path, criterion: str) -> tuple[str, str]:
    proc = _oracle("outcome", "--root", str(root), "--criterion", criterion)
    assert proc.returncode == 0, proc.stderr
    verdict, detail = proc.stdout.rstrip("\n").split("\t", 1)
    return verdict, detail


def test_the_v060_evidence_is_a_failure_that_names_pyannote(tmp_path: Path) -> None:
    root = _evidence(tmp_path / "d", groups_tsv=V060_GROUPS_TSV)
    verdict, detail = _outcome(root, "download")
    assert verdict == "fail"
    assert "pyannote(rc=1)" in detail
    assert "whisperx" not in detail


def test_a_full_clean_run_on_a_verified_image_passes(tmp_path: Path) -> None:
    clean = V060_GROUPS_TSV.replace("pyannote\t1", "pyannote\t0")
    assert _outcome(_evidence(tmp_path / "d", groups_tsv=clean), "download")[0] == "pass"


def test_a_group_that_exits_zero_with_errors_logged_fails(tmp_path: Path) -> None:
    """download_redaction_models returns 'partial' and the downloader exits 0."""
    clean = V060_GROUPS_TSV.replace("pyannote\t1", "pyannote\t0")
    partial = clean.replace("redaction\t0\t70", "redaction\t0\t70\t2")
    verdict, detail = _outcome(_evidence(tmp_path / "d", groups_tsv=partial), "download")
    assert verdict == "fail"
    assert "redaction(rc=0, 2 error line(s))" in detail
    zero_errors = clean.replace("redaction\t0\t70", "redaction\t0\t70\t0")
    assert _outcome(_evidence(tmp_path / "e", groups_tsv=zero_errors), "download")[0] == "pass"


def test_a_subset_run_is_not_measured(tmp_path: Path) -> None:
    subset = "nltk\t0\t3\nwhisperx\t0\t9\n"
    verdict, detail = _outcome(_evidence(tmp_path / "d", groups_tsv=subset), "download")
    assert verdict == "not-measured"
    assert "pyannote" in detail


def test_an_unverified_image_can_fail_but_never_pass(tmp_path: Path) -> None:
    clean = V060_GROUPS_TSV.replace("pyannote\t1", "pyannote\t0")
    unverified = "UNVERIFIED (DOWNLOADER_IMAGE override): davidamacey/x:v0.6.0"
    passing = _evidence(tmp_path / "a", groups_tsv=clean, provenance=unverified)
    failing = _evidence(tmp_path / "b", groups_tsv=V060_GROUPS_TSV, provenance=unverified)
    assert _outcome(passing, "download")[0] == "not-measured"
    assert _outcome(failing, "download")[0] == "fail"


@pytest.mark.parametrize("criterion", ["download", "weights", "nltk-hardlink"])
def test_missing_or_empty_evidence_is_not_measured(tmp_path: Path, criterion: str) -> None:
    root = _evidence(tmp_path / "d", groups_tsv=None)
    assert _outcome(root, criterion)[0] == "not-measured"
    name = {"download": "groups.tsv", "weights": "artifacts.tsv", "nltk-hardlink": "hardlink.tsv"}
    (root / name[criterion]).write_text("")
    assert _outcome(root, criterion)[0] == "not-measured"


def test_an_unrecognised_hardlink_verdict_is_not_a_pass(tmp_path: Path) -> None:
    root = _evidence(tmp_path / "d", groups_tsv=None)
    (root / "hardlink.tsv").write_text("ok\tlooked fine\n")
    assert _outcome(root, "nltk-hardlink")[0] == "not-measured"
    (root / "hardlink.tsv").write_text("fail\texited 0 but left 3 multiply-linked file(s)\n")
    assert _outcome(root, "nltk-hardlink") == (
        "fail",
        "exited 0 but left 3 multiply-linked file(s)",
    )


# ─── the scenario script's static contract ──────────────────────────────────


_MOUNT_TARGET = re.compile(r':(/[A-Za-z0-9_./-]+?)(?::ro)?"\s*\\?\s*$')


def _mount_targets(text: str) -> set[str]:
    return {
        m.group(1)
        for line in text.splitlines()
        if line.lstrip().startswith("-v ") and (m := _MOUNT_TARGET.search(line))
    }


def test_the_scenario_mounts_exactly_what_download_models_sh_mounts() -> None:
    sh = DOWNLOADER_SH.read_text()
    body = sh[sh.index("download_models_docker() {") :]
    body = body[: body.index("\n}\n")]
    scenario = SCENARIO.read_text()
    block = scenario[scenario.index("CACHE_MOUNTS=(") :]
    block = block[: block.index("\n)\n")]
    expected = _mount_targets(body)
    assert "/home/appuser/.cache/huggingface" in expected and "/models" in expected, expected
    assert _mount_targets(block) == expected


def test_the_scenario_is_cpu_only_versioned_and_never_echoes_the_token() -> None:
    code = "\n".join(line.split("#", 1)[0] for line in SCENARIO.read_text().splitlines())
    assert "docker run" in code
    assert "--gpus" not in code, "Scenario D must never request a GPU"
    assert "NVIDIA_VISIBLE_DEVICES=void" in code
    assert "torch.cuda.is_available()" in code
    assert re.search(r"-e HUGGINGFACE_TOKEN(\s|\\)", code), "pass the token by NAME"
    assert "-e HUGGINGFACE_TOKEN=" not in code
    assert "opentranscribe-backend:${LOCAL_IMAGE_TAG}" in code
    assert ":latest" not in code


def test_rehearse_declares_and_records_every_scenario_d_criterion() -> None:
    doc = yaml.safe_load(CRITERIA_YAML.read_text())
    declared = {c["id"]: c["severity"] for c in doc["stages"]["rehearse"]["criteria"]}
    for cid in COLD_CRITERIA:
        assert declared.get(cid) == "blocking", (cid, declared)
    code = REHEARSE_SH.read_text()
    assert code.index("# --- BEGIN cold-cache-scenario ---") < code.index(
        "Scenario A — fresh install"
    )


# ─── the REAL 65-rehearse.sh, in a scratch tree ─────────────────────────────


def _scratch(tmp_path: Path, cold_body: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    release_dir = repo / "scripts" / "release"
    tests_dir = repo / "scripts" / "release-tests"
    (tests_dir / "lib").mkdir(parents=True)
    release_dir.mkdir(parents=True)
    shutil.copy(REHEARSE_SH, release_dir / "65-rehearse.sh")
    shutil.copy(CRITERIA_LIB_SH, release_dir / "criteria-lib.sh")
    shutil.copy(CRITERIA_YAML, release_dir / "release-criteria.yaml")
    shutil.copy(ORACLE, tests_dir / "lib" / "cold-model-download.py")
    markers = tmp_path / "markers"
    markers.mkdir()
    for name in ("test-upgrade.sh", "test-lite-mode.sh"):
        (tests_dir / name).write_text("#!/bin/bash\nexit 0\n")
    (tests_dir / "test-fresh-install.sh").write_text(
        f'#!/bin/bash\necho "seed=${{OT_COLD_CACHE_SEED:-}} args=$*" >> {markers}/fresh.log\nexit 0\n'
    )
    (tests_dir / "test-cold-model-download.sh").write_text(
        f'#!/bin/bash\necho "ran $*" >> {markers}/cold.log\n{cold_body}\n'
    )
    for script in tests_dir.glob("*.sh"):
        script.chmod(0o755)
    (release_dir / "65-rehearse.sh").chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("#!/bin/bash\nexit 0\n")
    (bin_dir / "docker").chmod(0o755)
    return repo, markers


def _rehearse(repo: Path, env_extra: dict[str, str] | None = None) -> tuple[int, dict, str]:
    env = {
        "PATH": f"{repo.parent / 'bin'}:/usr/bin:/bin",
        "RELEASE_VERSION": "v9.9.9",
        "JSON_OUT": "true",
        "TO_BRANCH": "master",
        "COLD_CACHE_ROOT": str(repo.parent / "coldcache"),
        **(env_extra or {}),
    }
    proc = subprocess.run(
        [str(repo / "scripts/release/65-rehearse.sh"), "v9.9.9"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return proc.returncode, {c["id"]: c for c in payload["criteria"]}, proc.stderr


_WRITE_EVIDENCE = """
mkdir -p "$TEST_ROOT"
printf 'whisperx\\npyannote\\n' > "$TEST_ROOT/groups-expected.txt"
printf 'whisperx\\t0\\t1\\npyannote\\t{pyannote_rc}\\t1\\n' > "$TEST_ROOT/groups.tsv"
printf 'whisperx\\tpass\\t\\npyannote\\tpass\\t\\n' > "$TEST_ROOT/artifacts.tsv"
printf 'pass\\tnamed pathsec\\n' > "$TEST_ROOT/hardlink.tsv"
echo "verified img" > "$TEST_ROOT/provenance.txt"
exit {rc}
"""


def test_a_failing_group_fails_the_stage_even_when_the_scenario_exits_zero(
    tmp_path: Path,
) -> None:
    repo, markers = _scratch(tmp_path, _WRITE_EVIDENCE.format(pyannote_rc=1, rc=0))
    rc, criteria, stderr = _rehearse(repo)
    assert rc == 1, stderr
    assert criteria["cold-cache-model-download"]["outcome"] == "fail"
    assert "pyannote(rc=1)" in criteria["cold-cache-model-download"]["detail"]
    assert criteria["cold-cache-model-download"]["severity"] == "blocking"
    assert (markers / "fresh.log").read_text().startswith("seed= "), (
        "a failed cold download must not be handed to Scenario A as its model cache"
    )


def test_a_passing_cold_download_seeds_scenario_a(tmp_path: Path) -> None:
    repo, markers = _scratch(tmp_path, _WRITE_EVIDENCE.format(pyannote_rc=0, rc=0))
    rc, criteria, stderr = _rehearse(repo)
    assert rc == 0, stderr
    assert {criteria[c]["outcome"] for c in COLD_CRITERIA} == {"pass"}
    seed = str(tmp_path / "coldcache" / "cache")
    assert f"seed={seed} " in (markers / "fresh.log").read_text()
    assert (markers / "cold.log").read_text().strip() == "ran --yes"


def test_a_scenario_that_measured_nothing_is_a_precondition_not_a_pass(tmp_path: Path) -> None:
    repo, _ = _scratch(tmp_path, "exit 0")
    rc, criteria, stderr = _rehearse(repo)
    assert rc == 3, stderr
    assert {criteria[c]["outcome"] for c in COLD_CRITERIA} == {"not-measured"}
    assert {criteria[c]["status"] for c in COLD_CRITERIA} == {"fail"}


_WEIGHTS_FAIL = _WRITE_EVIDENCE.replace(
    "printf 'whisperx\\tpass\\t\\npyannote\\tpass\\t\\n'",
    "printf 'whisperx\\tpass\\t\\npyannote\\tfail\\tincomplete snapshot\\n'",
)


def test_a_cache_that_failed_the_weights_check_is_never_handed_to_scenario_a(
    tmp_path: Path,
) -> None:
    assert _WEIGHTS_FAIL != _WRITE_EVIDENCE
    repo, markers = _scratch(tmp_path, _WEIGHTS_FAIL.format(pyannote_rc=0, rc=0))
    rc, criteria, stderr = _rehearse(repo)
    assert rc == 1, stderr
    assert criteria["cold-cache-model-download"]["outcome"] == "pass"
    assert criteria["cold-cache-weights-present"]["outcome"] == "fail"
    assert (markers / "fresh.log").read_text().startswith("seed= ")


def test_scenario_d_runs_before_the_live_stack_is_stopped(tmp_path: Path) -> None:
    """D binds nothing, so its ~10 minutes must not be downtime for the live stack."""
    repo, markers = _scratch(tmp_path, _WRITE_EVIDENCE.format(pyannote_rc=0, rc=0))
    order = markers / "order.log"
    stopped = tmp_path / "stopped"
    (repo / "scripts/release-tests/test-cold-model-download.sh").write_text(
        f"#!/bin/bash\necho cold >> {order}\n" + _WRITE_EVIDENCE.format(pyannote_rc=0, rc=0)
    )
    (repo / "opentr.sh").write_text(
        f'#!/bin/bash\necho "opentr $1" >> {order}\n[[ "$1" == stop ]] && touch {stopped}\nexit 0\n'
    )
    (repo / "opentr.sh").chmod(0o755)
    # The live stack is "up" until opentr.sh stop has run.
    (tmp_path / "bin" / "docker").write_text(
        f'#!/bin/bash\nif [[ "$1" == ps && "$*" == *compose.project* && ! -e {stopped} ]]; then\n'
        "  echo live-backend\nfi\nexit 0\n"
    )
    env_extra = {"RELEASE_AUTO_STOP_STACK": "true"}
    rc, _, stderr = _rehearse(repo, env_extra)
    lines = order.read_text().split()
    assert "cold" in lines and "stop" in lines, (lines, stderr)
    assert lines.index("cold") < lines.index("stop"), lines


# ─── the scenario script itself, against a fake docker ──────────────────────

_FAKE_DOCKER = r"""#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
a = sys.argv[1:]
if not a or a[0] in ("stop", "ps"):
    sys.exit(0)
if a[:2] == ["image", "inspect"]:
    print("sha256:fake"); sys.exit(0)
if a[0] != "run":
    sys.exit(0)
name = a[a.index("--name") + 1] if "--name" in a else ""
mounts = {}
for i, tok in enumerate(a):
    if tok == "-v":
        src, dst = a[i + 1].split(":")[:2]
        mounts[dst] = Path(src)
nltk = mounts.get("/home/appuser/.cache/nltk_data")
suffix = name.split("-", 4)[-1]
log = Path(os.environ["FAKE_LOG"])
with log.open("a") as fh:
    fh.write(suffix + "\n")
if suffix == "cuda-probe":
    print("False"); sys.exit(0)
if suffix == "catalog":
    print("org/whisper"); sys.exit(0)
if suffix in os.environ.get("FAKE_HANG", "").split(","):
    time.sleep(60)
if suffix == "nltk":
    for res in ("tokenizers/punkt_tab", "corpora/stopwords", "corpora/words",
                "taggers/averaged_perceptron_tagger_eng", "chunkers/maxent_ne_chunker_tab"):
        (nltk / res).mkdir(parents=True, exist_ok=True)
        (nltk / res / "data.txt").write_text("x")
if suffix == "nltk-hardlinked":
    mode = os.environ.get("FAKE_HARDLINK", "pathsec")
    refusal = ("Failed to download {p}: Security Violation [pathsec.open]: refusing "
               "multiply-linked file '/home/appuser/.cache/nltk_data/x/{p}.zip' (st_nlink=2)")
    if mode == "repair":
        for f in nltk.rglob("*"):
            if f.is_file():
                data = f.read_bytes(); f.unlink(); f.write_bytes(data)
        sys.exit(0)
    print("❌   " + refusal.format(p="punkt_tab"))
    if mode == "mixed":
        print("❌   Failed to download words: HTTP Error 503: Service Unavailable")
    sys.exit(1)
sys.exit(0)
"""


def _run_scenario(tmp_path: Path, **env_extra: str) -> tuple[int, Path, str]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(_FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    root = tmp_path / "ot-reltest-coldcache-run"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TEST_ROOT": str(root),
        "OT_RELEASE_TEST_ALLOW_PATH": "1",
        "DOWNLOADER_IMAGE": "fake/backend:v9.9.9",
        "HUGGINGFACE_TOKEN": "hf_fake",
        "OT_COLD_CACHE_GROUPS": "whisperx,nltk",
        "FAKE_LOG": str(tmp_path / "docker.log"),
        **env_extra,
    }
    proc = subprocess.run(
        [str(SCENARIO), "--yes"], env=env, capture_output=True, text=True, timeout=120
    )
    return proc.returncode, root, proc.stdout + proc.stderr


def test_a_reused_root_is_refused_before_anything_runs(tmp_path: Path) -> None:
    root = tmp_path / "ot-reltest-coldcache-run"
    (root / "groups").mkdir(parents=True)
    (root / "groups" / "diar-native.row").write_text("diar-native\t0\t1\n")
    rc, _, out = _run_scenario(tmp_path)
    assert rc == 3, out
    assert "not empty" in out
    assert not (tmp_path / "docker.log").exists(), "a container ran against a warm root"


def test_a_hung_group_hits_its_deadline_and_fails(tmp_path: Path) -> None:
    rc, root, out = _run_scenario(tmp_path, FAKE_HANG="whisperx", OT_COLD_CACHE_GROUP_TIMEOUT_S="2")
    assert rc == 1, out
    row = (root / "groups.tsv").read_text().splitlines()[0].split("\t")
    assert row[:2] == ["whisperx", "124"], row
    assert _outcome(root, "download")[0] == "fail"


def test_the_hardlink_leg_never_links_the_cold_cache(tmp_path: Path) -> None:
    rc, root, out = _run_scenario(tmp_path)
    assert (root / "hardlink.tsv").read_text().startswith("pass\t"), out
    nltk = root / "cache" / "nltk_data"
    files = [f for f in nltk.rglob("*") if f.is_file()]
    assert files, "the fake nltk group wrote nothing"
    assert all(f.stat().st_nlink == 1 for f in files)
    assert not (root / "hardlink").exists()


@pytest.mark.parametrize(
    ("mode", "verdict"), [("pathsec", "pass"), ("repair", "pass"), ("mixed", "fail")]
)
def test_the_hardlink_leg_accepts_only_a_pure_pathsec_failure_or_a_repair(
    tmp_path: Path, mode: str, verdict: str
) -> None:
    _, root, out = _run_scenario(tmp_path, FAKE_HARDLINK=mode)
    assert (root / "hardlink.tsv").read_text().split("\t")[0] == verdict, out
