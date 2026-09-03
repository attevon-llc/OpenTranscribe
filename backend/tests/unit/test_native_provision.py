"""Startup provisioning of the native diarizer's model set (issues #654, #639).

``${MODEL_CACHE_DIR}/diar-native`` holds ONNX/PLDA graphs exported from the gated
``pyannote/speaker-diarization-community-1`` weights. Nothing in this repo produced that
directory before ``app.transcription.native_provision``: five files advertised a
``download-models diar-native`` command that did not exist, so every self-hosted install
came up green running in-process PyAnnote instead of the engine it was configured for.

Every test here drives a **real ``diar-server`` shim on PATH** through a real
``subprocess.run``, rather than patching ``subprocess``. The behaviour under test is the
translation of a typed exit code into an operator-facing outcome, so a patched call would
assert the mock rather than the translation. The shim records its argv and environment to
disk, which is also how the two properties that cannot be observed from the return value
are checked: that the token never reaches the command line, and that provisioning never
asks for a GPU.

Exit codes are upstream's stable contract (``crates/diar-core/src/provision/mod.rs::exit``)
and are reproduced here deliberately rather than imported, so a silent upstream renumbering
fails a test instead of changing behaviour unnoticed.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from app.transcription.native_provision import MARKER_FILENAME
from app.transcription.native_provision import ProvisionResult
from app.transcription.native_provision import ensure_native_models
from app.transcription.native_provision import read_marker

#: What a real export writes. Trimmed to the fields this module reads.
_GOOD_MARKER = {
    "schema": 1,
    "model_set": "fast",
    "exporter_version": 2,
    "with_gender": True,
    "toolchain": {"folder": "onnxslim", "gender_precision": "fp16"},
}


def _write_shim(
    directory: Path,
    *,
    exit_code: int = 0,
    stdout: str = "",
    marker: dict | None = None,
    sleep_s: float = 0.0,
) -> Path:
    """Install a fake ``diar-server`` that records how it was called.

    Returns the bin directory to prepend to PATH.
    """
    bindir = directory / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    record = directory / "record"
    record.mkdir(exist_ok=True)

    marker_line = ""
    if marker is not None:
        marker_json = json.dumps(marker)
        # The real binary writes the marker into --models-dir; mirror that so the
        # "did a marker exist before we ran" branch is exercised for real.
        marker_line = (
            f'python3 -c "import json,sys,os;'
            f"d=[a for i,a in enumerate(sys.argv[1:]) if sys.argv[i]=='--models-dir'][0];"
            f"open(os.path.join(d,'{MARKER_FILENAME}'),'w').write(sys.argv[-1])\" "
            f"\"$@\" '{marker_json}'"
        )

    script = f"""#!/bin/sh
printf '%s\\n' "$@" > "{record}/argv"
env > "{record}/env"
{f"sleep {sleep_s}" if sleep_s else ""}
{marker_line}
printf '%s' '{stdout}'
exit {exit_code}
"""
    path = bindir / "diar-server"
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def _recorded_argv(directory: Path) -> list[str]:
    return (directory / "record" / "argv").read_text(encoding="utf-8").splitlines()


def _recorded_env(directory: Path) -> dict[str, str]:
    raw = (directory / "record" / "env").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for line in raw.splitlines():
        key, _, value = line.partition("=")
        if _:
            out[key] = value
    return out


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    """A stand-in verification clip. Only its existence is checked by this module."""
    path = tmp_path / "smoke.wav"
    path.write_bytes(b"RIFF")
    return path


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, clip: Path) -> None:
    """Neutralise ambient configuration so a developer's .env cannot steer a test."""
    for key in (
        "DIAR_NATIVE_AUTO_PROVISION",
        "DIAR_NATIVE_MODEL_SET",
        "DIAR_NATIVE_PROVISION_TIMEOUT_S",
        "DIAR_MODELS_DIR",
        "DEPLOYMENT_MODE",
        "HF_ENDPOINT",
        "HUGGINGFACE_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DIAR_NATIVE_SMOKE_CLIP", str(clip))


class TestSkipPaths:
    """Deliberate non-attempts. All three must be non-fatal and say why."""

    def test_disabled_by_env_does_not_run_the_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bindir = _write_shim(tmp_path)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("DIAR_NATIVE_AUTO_PROVISION", "false")

        result = ensure_native_models(str(tmp_path))

        assert result.status == "skipped"
        # The point of the flag is that nothing runs, which the return value alone
        # cannot show — an export that ran and succeeded also reports no error.
        assert not (tmp_path / "record" / "argv").exists()

    def test_missing_binary_is_a_deployment_shape_not_a_fault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))

        result = ensure_native_models(str(tmp_path))

        assert result.status == "skipped"
        assert "diar-server" in result.reason

    def test_lite_skips_before_running_the_binary_it_does_carry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lite ships diar-server so it can serve, but not the export toolchain.

        Shelling out would exit 6 on every boot and advise a rebuild, which is the wrong
        remedy there — so the skip has to happen before the subprocess, and the presence
        of a working binary on PATH must not defeat it.
        """
        bindir = _write_shim(tmp_path, exit_code=0, marker=_GOOD_MARKER)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("DEPLOYMENT_MODE", "lite")

        result = ensure_native_models(str(tmp_path))

        assert result.status == "skipped"
        assert not (tmp_path / "record" / "argv").exists()
        assert "DIAR_NATIVE_MODELS_DIR" in result.reason

    def test_missing_smoke_clip_warns_rather_than_exporting_unverified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        bindir = _write_shim(tmp_path)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("DIAR_NATIVE_SMOKE_CLIP", str(tmp_path / "absent.wav"))

        with caplog.at_level(logging.WARNING):
            result = ensure_native_models(str(tmp_path))

        assert result.status == "skipped"
        assert not (tmp_path / "record" / "argv").exists()
        assert "clip" in caplog.text


class TestSuccessPaths:
    def test_export_reports_ok_and_not_already_provisioned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models = tmp_path / "models"
        models.mkdir()
        bindir = _write_shim(tmp_path, exit_code=0, marker=_GOOD_MARKER)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        result = ensure_native_models(str(models))

        assert result.status == "ok"
        assert result.already_provisioned is False
        assert read_marker(str(models)) is not None

    def test_valid_marker_short_circuits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The binary owns idempotency; this asserts we report it, not re-derive it."""
        models = tmp_path / "models"
        models.mkdir()
        (models / MARKER_FILENAME).write_text(json.dumps(_GOOD_MARKER), encoding="utf-8")
        bindir = _write_shim(tmp_path, exit_code=0)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        result = ensure_native_models(str(models))

        assert result.status == "ok"
        assert result.already_provisioned is True

    def test_force_reaches_the_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models = tmp_path / "models"
        models.mkdir()
        bindir = _write_shim(tmp_path, exit_code=0, marker=_GOOD_MARKER)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        ensure_native_models(str(models), force=True)

        assert "--force" in _recorded_argv(tmp_path)


class TestInvocationProperties:
    """Two properties that are invisible in the return value and matter operationally."""

    def test_provisioning_never_asks_for_a_gpu(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The export runs pipeline.to("cpu") and needs no accelerator.

        Letting it default to cuda is what used to brick GPU-less hosts, and on a
        single-GPU deployment it would contend with the workers for the only card.
        """
        models = tmp_path / "models"
        models.mkdir()
        bindir = _write_shim(tmp_path, exit_code=0, marker=_GOOD_MARKER)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        ensure_native_models(str(models))

        argv = _recorded_argv(tmp_path)
        assert argv[argv.index("--mode") + 1] == "cpu"

    def test_token_is_passed_by_environment_never_on_the_command_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """argv is world-readable via ``ps``; the environment is not."""
        models = tmp_path / "models"
        models.mkdir()
        bindir = _write_shim(tmp_path, exit_code=0, marker=_GOOD_MARKER)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_sentinel_value")

        ensure_native_models(str(models))

        assert "hf_sentinel_value" not in " ".join(_recorded_argv(tmp_path))
        assert _recorded_env(tmp_path).get("HUGGINGFACE_TOKEN") == "hf_sentinel_value"

    def test_blank_hf_endpoint_is_unset_rather_than_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """huggingface_hub reads HF_ENDPOINT with a default.

        A key that is set but empty resolves to "" and breaks the download with no
        useful error, so unset must beat blank.
        """
        models = tmp_path / "models"
        models.mkdir()
        bindir = _write_shim(tmp_path, exit_code=0, marker=_GOOD_MARKER)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("HF_ENDPOINT", "   ")

        ensure_native_models(str(models))

        assert "HF_ENDPOINT" not in _recorded_env(tmp_path)


class TestFailurePaths:
    """Every failure is reported, never raised: a degraded diarizer is not an outage."""

    @pytest.mark.parametrize(
        ("exit_code", "expected_in_reason"),
        [
            (5, "huggingface.co/pyannote/speaker-diarization-community-1"),
            (6, "requirements.txt"),
            (7, "read-write"),
            (9, "CPU"),
        ],
    )
    def test_exit_code_carries_an_actionable_remedy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        exit_code: int,
        expected_in_reason: str,
    ) -> None:
        models = tmp_path / "models"
        models.mkdir()
        bindir = _write_shim(tmp_path, exit_code=exit_code)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        result = ensure_native_models(str(models))

        assert result.status == "failed"
        assert result.exit_code == exit_code
        assert expected_in_reason in result.reason

    def test_binary_message_is_preserved_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models = tmp_path / "models"
        models.mkdir()
        payload = json.dumps({"status": "error", "exit_code": 5, "message": "gate denied"})
        bindir = _write_shim(tmp_path, exit_code=5, stdout=payload)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        result = ensure_native_models(str(models))

        assert "gate denied" in result.reason

    def test_a_hang_is_bounded_and_still_returns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models = tmp_path / "models"
        models.mkdir()
        bindir = _write_shim(tmp_path, exit_code=0, sleep_s=5)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("DIAR_NATIVE_PROVISION_TIMEOUT_S", "1")

        result = ensure_native_models(str(models))

        assert result.status == "failed"
        assert isinstance(result, ProvisionResult)


class TestGenderPrecisionGuard:
    """The one export defect that raises no error of its own.

    Without ``onnxconverter_common`` the gender classifier silently exports at fp32 —
    379 MB instead of 189 MB, roughly 500 MiB of extra VRAM for the life of the
    deployment — and the export still exits 0. The marker is the only place that is
    visible, so this warning is the whole detection mechanism.
    """

    def test_fp32_gender_export_is_called_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        models = tmp_path / "models"
        models.mkdir()
        fp32 = {
            **_GOOD_MARKER,
            "toolchain": {"folder": "onnxslim", "gender_precision": "fp32"},
        }
        bindir = _write_shim(tmp_path, exit_code=0, marker=fp32)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        with caplog.at_level(logging.WARNING):
            result = ensure_native_models(str(models))

        assert result.status == "ok"  # it did export; it is just the wrong precision
        assert "onnxconverter-common" in caplog.text

    def test_fp16_gender_export_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        models = tmp_path / "models"
        models.mkdir()
        bindir = _write_shim(tmp_path, exit_code=0, marker=_GOOD_MARKER)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        with caplog.at_level(logging.WARNING):
            ensure_native_models(str(models))

        assert "onnxconverter-common" not in caplog.text


class TestReadMarker:
    def test_absent_marker_is_none(self, tmp_path: Path) -> None:
        assert read_marker(str(tmp_path)) is None

    def test_unparseable_marker_is_none(self, tmp_path: Path) -> None:
        (tmp_path / MARKER_FILENAME).write_text("{not json", encoding="utf-8")
        assert read_marker(str(tmp_path)) is None

    def test_non_object_marker_is_none(self, tmp_path: Path) -> None:
        """A bare JSON scalar parses but is not a marker."""
        (tmp_path / MARKER_FILENAME).write_text("42", encoding="utf-8")
        assert read_marker(str(tmp_path)) is None
