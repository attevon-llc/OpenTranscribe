"""`./opentr.sh start --gpu-scale-default-worker 0|1` must beat `.env`.

docker-compose.gpu-scale.yml scales the default celery-worker with
``${GPU_SCALE_DEFAULT_WORKER:-0}`` and opentr.sh does ``set -a; source ./.env``, so a
caller's ``GPU_SCALE_DEFAULT_WORKER=0`` is overwritten by a ``.env`` that says 1. The cross-card
diar-native tests need 0 (no default worker on the gpu queue) and editing the shared ``.env`` is
off limits, so the flag is applied after ``.env``, like ``--gpu-device``.
"""

from __future__ import annotations

from tests.unit import test_opentr_gpu_device_flag as _base

# Same hermetic checkout (stub docker/nvidia-smi, .env that clobbers exports) as --gpu-device.
sandbox = _base.sandbox
_run = _base._run

START = ("start", "dev", "--no-nas", "--dry-run")


def _set_dotenv_default_worker(sandbox_dir, value: str) -> None:
    env = sandbox_dir.checkout / ".env"
    env.write_text(env.read_text() + f"GPU_SCALE_DEFAULT_WORKER={value}\n")


def _record_scale_in_docker_stub(sandbox_dir) -> None:
    stub = sandbox_dir.stubs / "docker"
    stub.write_text(
        stub.read_text().replace(
            "echo nvidia",
            f'echo "SCALE=${{GPU_SCALE_DEFAULT_WORKER:-unset}}" >> {sandbox_dir.docker_env}\n'
            "echo nvidia",
        )
    )


def test_control_a_pre_export_is_clobbered_by_dotenv(sandbox):
    """Reproduces the defect: without the flag, .env's 1 wins over the caller's 0."""
    _set_dotenv_default_worker(sandbox, "1")
    _record_scale_in_docker_stub(sandbox)
    proc, output = _run(sandbox, *START, "--gpu-scale", extra_env={"GPU_SCALE_DEFAULT_WORKER": "0"})
    assert proc.returncode == 0, output
    assert "SCALE=1" in sandbox.docker_env.read_text()


def test_flag_beats_dotenv_reaches_compose_and_is_in_the_banner(sandbox):
    _set_dotenv_default_worker(sandbox, "1")
    _record_scale_in_docker_stub(sandbox)
    proc, output = _run(sandbox, *START, "--gpu-scale", "--gpu-scale-default-worker", "0")
    assert proc.returncode == 0, output
    assert "--gpu-scale-default-worker 0: default celery-worker scale=0" in output
    assert "SCALE=0" in sandbox.docker_env.read_text()


def test_value_other_than_0_or_1_exits_2(sandbox):
    proc, output = _run(sandbox, *START, "--gpu-scale", "--gpu-scale-default-worker", "2")
    assert proc.returncode == 2, output
    assert "must be 0 or 1" in output


def test_missing_value_exits_2(sandbox):
    proc, output = _run(sandbox, *START, "--gpu-scale", "--gpu-scale-default-worker")
    assert proc.returncode == 2, output
    assert "unbound variable" not in output
    assert "must be 0 or 1" in output


def test_requires_gpu_scale(sandbox):
    proc, output = _run(sandbox, *START, "--gpu-scale-default-worker", "0")
    assert proc.returncode == 2, output
    assert "only valid together with --gpu-scale" in output


def test_help_documents_the_flag(sandbox):
    _, output = _run(sandbox, "help")
    assert "--gpu-scale-default-worker" in output
