"""A stale ENGINE_SHARED_VOLUME_PATH must not silently disable the handoff (#661 E2).

E2 folded the ``transcription-temp`` volume into a namespace of ``pipeline_scratch`` and
moved the coded default to ``/scratch/opentranscribe/engine``. But ``.env`` beats a coded
default, and every install created before that change carries
``ENGINE_SHARED_VOLUME_PATH=/tmp/transcription`` from the old ``.env.example`` — naming a
volume that no longer exists.

Measured on a live upgraded stack before this fix::

    GPU task: shared-volume WAV path '/tmp/transcription/db4fd5aa-....wav' recorded by
    preprocess but NOT FOUND on this container for file 339855 — falling back to MinIO

``os.makedirs`` had recreated the stale path *inside the writer's container*, so the write
"succeeded", the reader looked in the real mount, found nothing, and every job silently took
the slow path. That is exactly the regression E0's logging exists to surface — and it did.

These tests pin the resolver, not the log line: the log only tells you afterwards.
"""

from __future__ import annotations

import logging

import pytest

from app.core.constants import ENGINE_SHARED_VOLUME_DEFAULT
from app.core.constants import resolve_engine_shared_volume_path


def test_unset_uses_the_coded_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENGINE_SHARED_VOLUME_PATH", raising=False)
    assert resolve_engine_shared_volume_path() == ENGINE_SHARED_VOLUME_DEFAULT


def test_a_configured_path_that_exists_is_honoured(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An operator who deliberately relocates the handoff keeps their value."""
    custom = tmp_path / "custom-handoff"
    custom.mkdir()
    monkeypatch.setenv("ENGINE_SHARED_VOLUME_PATH", str(custom))
    assert resolve_engine_shared_volume_path() == str(custom)


def test_a_stale_path_falls_back_to_the_default_that_does_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog
) -> None:
    """The upgrade case: .env names the removed volume, the real mount is present."""
    real_default = tmp_path / "scratch" / "engine"
    real_default.mkdir(parents=True)
    monkeypatch.setattr(
        "app.core.constants.ENGINE_SHARED_VOLUME_DEFAULT", str(real_default), raising=True
    )
    monkeypatch.setenv("ENGINE_SHARED_VOLUME_PATH", str(tmp_path / "tmp" / "transcription"))

    with caplog.at_level(logging.WARNING):
        resolved = resolve_engine_shared_volume_path()

    assert resolved == str(real_default), (
        "a configured path that does not exist must not win over a default that does — "
        "os.makedirs would recreate it container-local and silently kill the fast path"
    )
    assert "does not exist in this container" in caplog.text, (
        "the fallback must be loud; a silent correction is how the original bug hid"
    )


def test_neither_existing_honours_the_operator_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """With no real mount either way, do not invent a path the operator did not choose.

    The caller's own mkdir/not-found logging is then the accurate signal, rather than this
    resolver quietly substituting a directory that is equally absent.
    """
    monkeypatch.setattr(
        "app.core.constants.ENGINE_SHARED_VOLUME_DEFAULT", str(tmp_path / "absent"), raising=True
    )
    configured = str(tmp_path / "also-absent")
    monkeypatch.setenv("ENGINE_SHARED_VOLUME_PATH", configured)
    assert resolve_engine_shared_volume_path() == configured


def test_every_reader_and_writer_goes_through_the_resolver() -> None:
    """No module may read the raw env var — that is how the three sites drifted before.

    E0 was originally caused by preprocess.py, engine/config.py and diarizer_native.py each
    holding their own default. Unifying the DEFAULT was not enough; they must also share the
    staleness check, or one of them honours a dead path again.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for py in root.rglob("*.py"):
        if py.name == "constants.py":
            continue  # the resolver itself legitimately reads it
        text = py.read_text(encoding="utf-8")
        if 'environ.get("ENGINE_SHARED_VOLUME_PATH"' in text or (
            'getenv("ENGINE_SHARED_VOLUME_PATH"' in text
        ):
            offenders.append(str(py.relative_to(root)))
    assert not offenders, (
        f"these modules read ENGINE_SHARED_VOLUME_PATH directly instead of calling "
        f"resolve_engine_shared_volume_path(): {offenders}. A raw read honours a stale "
        f"value naming the removed transcription-temp volume, which os.makedirs then "
        f"recreates container-local — silently disabling the handoff (issue #661 E2)."
    )
