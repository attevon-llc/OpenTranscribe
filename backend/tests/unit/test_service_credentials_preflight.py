"""Operator preflight for the production FLOWER_PASSWORD / MinIO-encryption startup checks.

The backend refuses to start (startup_validation.validate_service_credentials) on a default
Flower password or bundled MinIO without KMS auto-encryption. An older production .env that
sets neither would crash-loop after `opentranscribe.sh update`; scripts/lib/service-credentials.sh
refuses first, with a remedy, while the old stack is still running.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "scripts" / "lib" / "service-credentials.sh"
STRONG = "FLOWER_PASSWORD=0123456789abcdef0123456789abcdef\n"
KMS = "MINIO_KMS_SECRET_KEY=opentranscribe-key:abc\nMINIO_KMS_AUTO_ENCRYPTION=on\n"


def _run(tmp_path: Path, env_text: str) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / "scratch.env"
    env_file.write_text(env_text)
    return subprocess.run(  # nosec B603 B607  fixed argv, hermetic temp file
        ["bash", "-c", f'. "{LIB}"; ot_require_service_credentials "{env_file}"'],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_weak_flower_password_is_refused(tmp_path: Path) -> None:
    for weak in ("flower", "changeme", "CHANGE_ME_x", ""):
        r = _run(tmp_path, f"FLOWER_PASSWORD={weak}\n{KMS}")
        assert r.returncode == 1, weak
        assert "FLOWER_PASSWORD" in r.stdout
        assert "openssl rand -hex 16" in r.stdout


def test_missing_flower_password_is_refused(tmp_path: Path) -> None:
    r = _run(tmp_path, KMS)
    assert r.returncode == 1
    assert "FLOWER_PASSWORD" in r.stdout


def test_missing_encryption_is_refused_with_key_backup_warning(tmp_path: Path) -> None:
    r = _run(tmp_path, STRONG)
    assert r.returncode == 1
    assert "MINIO_KMS_AUTO_ENCRYPTION=on" in r.stdout
    assert "BACK UP" in r.stdout
    assert "FLOWER_PASSWORD" not in r.stdout.split("Refusing")[0]


def test_never_writes_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "scratch.env"
    env_file.write_text("REDIS_PASSWORD=x\n")
    subprocess.run(
        ["bash", "-c", f'. "{LIB}"; ot_require_service_credentials "{env_file}"'],  # nosec B603 B607
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert env_file.read_text() == "REDIS_PASSWORD=x\n"


def test_both_set_passes_silently(tmp_path: Path) -> None:
    r = _run(tmp_path, STRONG + KMS)
    assert r.returncode == 0
    assert r.stdout == ""


def test_explicit_unencrypted_opt_out_passes(tmp_path: Path) -> None:
    r = _run(tmp_path, STRONG + "ALLOW_UNENCRYPTED_OBJECT_STORE=true\n")
    assert r.returncode == 0


def test_opt_out_does_not_excuse_a_weak_flower_password(tmp_path: Path) -> None:
    r = _run(tmp_path, "FLOWER_PASSWORD=flower\nALLOW_UNENCRYPTED_OBJECT_STORE=true\n")
    assert r.returncode == 1


def test_non_minio_storage_needs_no_kms(tmp_path: Path) -> None:
    assert _run(tmp_path, STRONG + "STORAGE_BACKEND=s3\n").returncode == 0


def test_development_environment_is_exempt(tmp_path: Path) -> None:
    assert _run(tmp_path, "ENVIRONMENT=development\n").returncode == 0


def test_secret_containing_hash_is_not_truncated(tmp_path: Path) -> None:
    assert _run(tmp_path, "FLOWER_PASSWORD=flower #x\n" + KMS).returncode == 0


def test_opentranscribe_sh_wires_preflight_into_start_and_update() -> None:
    text = (REPO / "opentranscribe.sh").read_text()
    assert "scripts/lib/service-credentials.sh" in text
    pre = text.split("preflight_upgrade_env() {", 1)[1].split("\n}\n", 1)[0]
    assert "ot_require_service_credentials .env || return 1" in pre
    start = text.split("    start)\n", 1)[1].split("        ;;", 1)[0]
    assert "ot_require_service_credentials .env || exit 1" in start


def test_rehearsal_envs_carry_both_settings() -> None:
    tmpl = (REPO / "scripts/release-tests/lib/env-template.sh").read_text()
    for needle in (
        "FLOWER_PASSWORD=",
        "MINIO_KMS_SECRET_KEY=opentranscribe-key:",
        "MINIO_KMS_AUTO_ENCRYPTION=on",
    ):
        assert needle in tmpl
    assert (
        "$(et_service_credentials)" in (REPO / "scripts/release-tests/test-upgrade.sh").read_text()
    )
    assert "$(et_service_credentials)" in tmpl
