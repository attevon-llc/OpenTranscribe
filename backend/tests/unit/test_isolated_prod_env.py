"""scripts/release-tests/isolated-prod-env.sh builds a generated prod config, never the operator's .env.

Drives the REAL script inside a throwaway git repo with stub ``opentr.sh``, cert scripts and
stack-target.sh; proves the generated .env, the fresh-project start line and the teardown.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "release-tests" / "isolated-prod-env.sh"
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent"}


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel in (
        "scripts/release-tests/isolated-prod-env.sh",
        "scripts/release-tests/lib/env-template.sh",
        "scripts/lib/model-cache-dir.sh",
        "scripts/release/fresh-stack-lib.sh",
    ):
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, dest)
    log = root / "calls.log"
    (root / ".env.example").write_text(
        "POSTGRES_PASSWORD=CHANGE_ME_auto_generated_on_install\n"
        "FLOWER_PASSWORD=CHANGE_ME_auto_generated_on_install\n"
        "MINIO_KMS_SECRET_KEY=CHANGE_ME_auto_generated_on_install\n"
        "MODEL_CACHE_DIR=./models\nDEMO_MODE=false\n# ENVIRONMENT=production\n"
    )
    # records what the isolated models dir held when `start` ran: the sidecar reads it at boot
    _write(
        root / "opentr.sh",
        f'#!/bin/bash\necho "opentr $*" >> "{log}"\n'
        f'for d in .fresh/*/diar-native-models; do [ -d "$d" ] && echo "models $d: $(ls $d)" >> "{log}"; done\n'
        "exit 0\n",
    )
    _write(root / "scripts" / "generate-ssl-cert.sh", f'#!/bin/bash\necho ssl >> "{log}"\n')
    _write(
        root / "scripts" / "pki" / "setup-test-pki.sh",
        f'#!/bin/bash\necho pki >> "{log}"\nmkdir -p scripts/pki/test-certs\n'
        'echo "PKI_TRUSTED_PROXIES=${FAKE_FRAGMENT:-127.0.0.1/32,172.16.0.0/12}"'
        " > scripts/pki/test-certs/pki-test.env\n",
    )
    # stand-in for the generator: --print-trusted-proxies answers what the project network gives
    _write(
        root / "scripts" / "pki" / "generate-test-env.sh",
        f'#!/bin/bash\necho "proj=$COMPOSE_PROJECT_NAME $*" >> "{log}"\n'
        'echo "${FAKE_DERIVED_PROXIES:-127.0.0.1/32,10.200.2.0/24}"\n',
    )
    (root / "frontend" / "static" / "fonts").mkdir(parents=True)
    (root / "frontend" / "static" / "fonts" / "Poppins-Bold.woff2").write_text("font")
    _write(
        root / "scripts" / "lib" / "stack-target.sh",
        "st_target() { return 0; }\nst_require() { return 0; }\nst_container() { echo c; }\n",
    )
    # the pki tail waits on curl then runs pytest from backend/venv: stub both
    _write(root / "bin" / "curl", "#!/bin/bash\nexit 0\n")
    _write(root / "backend" / "venv" / "bin" / "python", "#!/bin/bash\nexit 0\n")
    # the operator's .env: must never be copied into the isolated tree
    (root / ".env").write_text("FLOWER_PASSWORD=OPERATOR_SENTINEL\n")
    (root / ".gitignore").write_text(
        ".env\ncalls.log\nfrontend/static/fonts\nscripts/pki/test-certs\n"
    )
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init"],
        cwd=root,
        check=True,
        env=env,
    )
    return root


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def _run(
    root: Path, *args: str, cache: Path, seed: bool = True
) -> subprocess.CompletedProcess[str]:
    if seed and args[0] == "run":
        (cache / "diar-native").mkdir(parents=True, exist_ok=True)
        (cache / "diar-native" / "segmentation-3.0.onnx").write_text("onnx")
    env = {
        **os.environ,
        **GIT_ENV,
        "MODEL_CACHE_DIR": str(cache),
        "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", str(root / "scripts/release-tests/isolated-prod-env.sh"), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_prepare_generates_production_grade_env(fake_repo: Path, tmp_path: Path) -> None:
    cache = tmp_path / "models"
    res = _run(fake_repo, "prepare", "demo", cache=cache)
    assert res.returncode == 0, res.stderr
    tree = Path(res.stdout.strip().splitlines()[-1])
    text = (tree / ".env").read_text()
    assert "OPERATOR_SENTINEL" not in text
    assert "CHANGE_ME" not in text
    assert "MINIO_KMS_AUTO_ENCRYPTION=on" in text
    assert "DEMO_MODE=true" in text
    assert f"MODEL_CACHE_DIR={cache}" in text
    assert "ENVIRONMENT=production" in text
    flower = next(line for line in text.splitlines() if line.startswith("FLOWER_PASSWORD="))
    assert len(flower.split("=", 1)[1]) >= 16


def test_prepare_keeps_the_kms_key_padding(fake_repo: Path, tmp_path: Path) -> None:
    """MinIO refuses a key whose base64 lost its trailing '=' (illegal base64 at byte 40)."""
    res = _run(fake_repo, "prepare", "pki", cache=tmp_path / "models")
    assert res.returncode == 0, res.stderr
    tree = Path(res.stdout.strip().splitlines()[-1])
    line = next(
        line
        for line in (tree / ".env").read_text().splitlines()
        if line.startswith("MINIO_KMS_SECRET_KEY=")
    )
    name, _, b64 = line.split("=", 1)[1].partition(":")
    assert name == "opentranscribe-key"
    assert len(base64.b64decode(b64, validate=True)) == 32


def test_prepared_tree_is_an_attributable_checkout_of_head(fake_repo: Path, tmp_path: Path) -> None:
    """opentr.sh stamps GIT_SHA from git in the built tree; a plain copy stamps 'unknown'."""
    res = _run(fake_repo, "prepare", "pki", cache=tmp_path / "models")
    assert res.returncode == 0, res.stderr
    tree = Path(res.stdout.strip().splitlines()[-1])
    assert _git(tree, "rev-parse", "HEAD") == _git(fake_repo, "rev-parse", "HEAD")
    # generated .env and certs must not make opentr.sh stamp the build "-dirty"
    assert _git(tree, "status", "--porcelain") == ""


def test_uncommitted_tracked_change_refuses_with_exit_3(fake_repo: Path, tmp_path: Path) -> None:
    with (fake_repo / ".env.example").open("a") as fh:
        fh.write("EXTRA=1\n")
    res = _run(fake_repo, "prepare", "pki", cache=tmp_path / "models")
    assert res.returncode == 3, res.stdout + res.stderr
    assert "uncommitted" in res.stderr


def test_run_pki_starts_fresh_project_and_stops_it(fake_repo: Path, tmp_path: Path) -> None:
    res = _run(fake_repo, "run", "demo", "--name", "t1", "--", "--with-mock-llm", cache=tmp_path)
    assert res.returncode == 0, res.stderr
    calls = (fake_repo / "calls.log").read_text().splitlines()
    # the tree copy has its own stubs but the log path is absolute, so both land in one file
    start = next(c for c in calls if c.startswith("opentr start"))
    assert "prod --build --fresh t1" in start
    assert "--lite --demo" in start and "--with-mock-llm" in start
    assert "opentr stop --fresh t1" in calls


def test_misuse_is_exit_2(fake_repo: Path, tmp_path: Path) -> None:
    assert _run(fake_repo, "run", "bogus", cache=tmp_path).returncode == 2


def test_prepare_provisions_the_gitignored_fonts(fake_repo: Path, tmp_path: Path) -> None:
    """The clone has no gitignored fonts; stack_target asset:frontend-fonts needs a woff2."""
    res = _run(fake_repo, "prepare", "pki", cache=tmp_path / "models")
    assert res.returncode == 0, res.stderr
    tree = Path(res.stdout.strip().splitlines()[-1])
    assert (tree / "frontend/static/fonts/Poppins-Bold.woff2").read_text() == "font"
    assert _git(tree, "status", "--porcelain") == ""


def test_pki_restarts_once_when_network_subnet_differs_from_fallback(
    fake_repo: Path, tmp_path: Path
) -> None:
    """First start falls back to 172.16/12; the fresh network is elsewhere, so start again."""
    res = _run(fake_repo, "run", "pki", "--name", "t2", cache=tmp_path)
    assert res.returncode == 0, res.stderr
    calls = (fake_repo / "calls.log").read_text().splitlines()
    assert any("proj=otfresh-t2 --print-trusted-proxies" in c for c in calls), calls
    assert len([c for c in calls if c.startswith("opentr start")]) == 2, calls


def test_pki_does_not_restart_when_trust_already_matches(
    fake_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FRAGMENT", "127.0.0.1/32,10.200.2.0/24")
    res = _run(fake_repo, "run", "pki", "--name", "t3", cache=tmp_path)
    assert res.returncode == 0, res.stderr
    calls = (fake_repo / "calls.log").read_text().splitlines()
    assert len([c for c in calls if c.startswith("opentr start")]) == 1, calls


def test_run_seeds_the_isolated_diar_native_dir_before_start(
    fake_repo: Path, tmp_path: Path
) -> None:
    """Must fire: an empty .fresh/NAME/diar-native-models crash-loops the sidecar (exit 8)."""
    res = _run(fake_repo, "run", "pki", "--name", "Seed1", cache=tmp_path)
    assert res.returncode == 0, res.stderr
    log = (fake_repo / "calls.log").read_text()
    assert "diar-native-models: segmentation-3.0.onnx" in log


def test_run_refuses_without_a_diar_native_export_to_seed_from(
    fake_repo: Path, tmp_path: Path
) -> None:
    res = _run(fake_repo, "run", "pki", "--name", "Seed2", cache=tmp_path, seed=False)
    assert res.returncode == 3
    assert "no diar-native export" in res.stderr
    log = fake_repo / "calls.log"
    assert not log.exists() or "opentr start" not in log.read_text()


def test_demo_seed_runs_with_demo_mode_off_for_that_process_only(
    fake_repo: Path, tmp_path: Path
) -> None:
    """The seeder refuses under DEMO_MODE; demo-reset.sh passes `-e DEMO_MODE=false` (S6HB)."""
    log = fake_repo / "calls.log"
    _write(fake_repo / "bin" / "docker", f'#!/bin/bash\necho "docker $*" >> "{log}"\nexit 0\n')
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    res = _run(fake_repo, "run", "demo", "--name", "Seed3", "--bundle", str(bundle), cache=tmp_path)
    assert res.returncode == 0, res.stderr
    seed = [x for x in log.read_text().splitlines() if "demo_seed import" in x]
    assert len(seed) == 1 and "exec -e DEMO_MODE=false" in seed[0], seed
