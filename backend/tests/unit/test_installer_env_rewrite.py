"""setup-opentranscribe.sh writes its generated values into .env without touching other keys.

The installer rewrites a copy of the shipped .env.example with sed. Unanchored patterns such
as ``BATCH_SIZE=.*`` also matched SEARCH_BULK_BATCH_SIZE, DIARIZATION_BATCH_SIZE and
GPU_DEFAULT_BATCH_SIZE, and ``GPU_DEVICE_ID=.*`` matched REDACTION_GPU_DEVICE_ID, so a fresh
install silently changed unrelated settings. The shell functions are extracted from the real
script and executed against the real .env.example.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit._shell_extract import extract_function

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP = REPO_ROOT / "setup-opentranscribe.sh"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

FUNCS = (
    "_sed_env_inplace",
    "_replace_env_value",
    "_append_env_line",
    "_upsert_env",
    "_create_initial_env",
    "_write_hardware_settings",
)


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    source = "\n".join(extract_function(SETUP, name) for name in FUNCS)
    script = "\n".join(
        [
            "ensure_env_permissions() { :; }",
            "print_success() { :; }",
            source,
            body,
        ]
    )
    return subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False
    )


def _parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            out[key] = val
    return out


def test_fresh_env_only_changes_the_keys_the_installer_sets(tmp_path):
    (tmp_path / ".env.example").write_text(ENV_EXAMPLE.read_text())
    before = _parse(ENV_EXAMPLE)
    result = _run(
        tmp_path,
        "POSTGRES_PASSWORD=pg MINIO_ROOT_PASSWORD=mn JWT_SECRET=jw ENCRYPTION_KEY=en "
        "REDIS_PASSWORD=rd OPENSEARCH_PASSWORD=os OPENSEARCH_ADMIN_PASSWORD=oa "
        "FLOWER_PASSWORD=fl MINIO_KMS_KEY=km BATCH_SIZE=7 COMPUTE_TYPE=int8 "
        "DETECTED_DEVICE=cuda GPU_DEVICE_ID=2; "
        "_create_initial_env; _write_hardware_settings",
    )
    assert result.returncode == 0, result.stderr
    after = _parse(tmp_path / ".env")
    intended = {
        "POSTGRES_PASSWORD",
        "MINIO_ROOT_PASSWORD",
        "JWT_SECRET_KEY",
        "ENCRYPTION_KEY",
        "REDIS_PASSWORD",
        "OPENSEARCH_PASSWORD",
        "OPENSEARCH_ADMIN_PASSWORD",
        "FLOWER_PASSWORD",
        "MINIO_KMS_SECRET_KEY",
        "BATCH_SIZE",
        "COMPUTE_TYPE",
        "MODEL_CACHE_DIR",
        "OT_IMAGE_TAG",
        "USE_GPU",
        "GPU_DEVICE_ID",
        "TORCH_DEVICE",
        "DETECTED_DEVICE",
        "USE_NVIDIA_RUNTIME",
        "FORCE_CPU_MODE",
    }
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed <= intended, f"installer rewrote unrelated keys: {sorted(changed - intended)}"
    assert after["BATCH_SIZE"] == "7"
    assert after["GPU_DEVICE_ID"] == "2"
    assert after["REDACTION_GPU_DEVICE_ID"] == before["REDACTION_GPU_DEVICE_ID"]
    assert after["SEARCH_BULK_BATCH_SIZE"] == before["SEARCH_BULK_BATCH_SIZE"]


def test_upsert_terminates_an_unterminated_last_line(tmp_path):
    (tmp_path / ".env").write_text("A=1\nLAST=value")
    result = _run(tmp_path, "_upsert_env NEW two")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".env").read_text() == "A=1\nLAST=value\nNEW=two\n"


def test_replace_escapes_sed_metacharacters_and_keeps_a_user_backup(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=old\n")
    (tmp_path / ".env.bak").write_text("keep me\n")
    result = _run(tmp_path, "_upsert_env TOKEN 'a&b|c\\d'")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".env").read_text() == "TOKEN=a&b|c\\d\n"
    assert (tmp_path / ".env.bak").read_text() == "keep me\n"
