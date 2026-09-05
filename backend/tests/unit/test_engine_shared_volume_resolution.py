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
    # The known callers of resolve_engine_shared_volume_path() at the time this test was
    # written. A scan that silently walked the wrong root (or aborted early) would still
    # report zero offenders — because "continue" on constants.py was the *only* branch ever
    # exercised on a green run — so the scan must prove it actually reached real files,
    # including these, not merely that it found no violations.
    known_readers = {
        "tasks/transcription/preprocess.py",
        "transcription/diarizer_native.py",
        "transcription/engine/config.py",
    }
    seen_readers: set[str] = set()
    files_scanned = 0
    offenders = []
    for py in root.rglob("*.py"):
        if py.name == "constants.py":
            continue  # the resolver itself legitimately reads it
        files_scanned += 1
        rel = str(py.relative_to(root))
        if rel in known_readers:
            seen_readers.add(rel)
        text = py.read_text(encoding="utf-8")
        if 'environ.get("ENGINE_SHARED_VOLUME_PATH"' in text or (
            'getenv("ENGINE_SHARED_VOLUME_PATH"' in text
        ):
            offenders.append(rel)
    assert files_scanned > 100, (
        f"scanned only {files_scanned} files under {root} — this scan can pass having "
        f"examined nothing, so a suspiciously small count must fail loudly instead of "
        f"silently certifying an empty tree"
    )
    assert seen_readers == known_readers, (
        f"the scan never reached {known_readers - seen_readers} of the known callers of "
        f"resolve_engine_shared_volume_path() — a scan that never visits the real reader "
        f"files would still report zero offenders"
    )
    assert not offenders, (
        f"these modules read ENGINE_SHARED_VOLUME_PATH directly instead of calling "
        f"resolve_engine_shared_volume_path(): {offenders}. A raw read honours a stale "
        f"value naming the removed transcription-temp volume, which os.makedirs then "
        f"recreates container-local — silently disabling the handoff (issue #661 E2)."
    )


def test_a_present_but_stale_configured_dir_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog
) -> None:
    """The actual D4 bug: a stale configured dir is not merely ABSENT, it EXISTS.

    ``preprocess.py`` used to ``os.makedirs`` the configured path unconditionally, so by the
    time anything calls this resolver the stale ``/tmp/transcription`` already exists again —
    freshly recreated inside the writer's own writable layer. ``os.path.isdir`` alone cannot
    tell that apart from the real shared mount; only a device comparison can.
    """
    real_default = tmp_path / "scratch" / "engine"
    real_default.mkdir(parents=True)
    stale_but_present = tmp_path / "tmp" / "transcription"
    stale_but_present.mkdir(parents=True)  # already recreated by the writer's os.makedirs

    monkeypatch.setattr(
        "app.core.constants.ENGINE_SHARED_VOLUME_DEFAULT", str(real_default), raising=True
    )
    monkeypatch.setenv("ENGINE_SHARED_VOLUME_PATH", str(stale_but_present))

    devices = {str(real_default): 111, str(stale_but_present): 222}
    monkeypatch.setattr("app.core.constants._stat_dev", lambda p: devices.get(p), raising=True)

    with caplog.at_level(logging.WARNING):
        resolved = resolve_engine_shared_volume_path()

    assert resolved == str(real_default), (
        "a PRESENT-but-stale configured directory must not silently win just because "
        "os.path.isdir succeeds on it — that ambiguity is exactly how issue #661 hid, and "
        "the previous resolver's loud-warning branch only fired on ABSENCE"
    )
    assert "not on the same filesystem" in caplog.text, (
        "the fallback must be loud even when the stale directory exists — a silent "
        "correction here is indistinguishable from the original silent bug"
    )


def test_a_present_configured_dir_on_the_real_mount_is_honoured(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog
) -> None:
    """The device check must not turn every relocation into a false positive."""
    real_default = tmp_path / "scratch" / "engine"
    real_default.mkdir(parents=True)
    configured = tmp_path / "also-on-the-volume"
    configured.mkdir()

    monkeypatch.setattr(
        "app.core.constants.ENGINE_SHARED_VOLUME_DEFAULT", str(real_default), raising=True
    )
    monkeypatch.setenv("ENGINE_SHARED_VOLUME_PATH", str(configured))
    monkeypatch.setattr("app.core.constants._stat_dev", lambda p: 999, raising=True)

    with caplog.at_level(logging.WARNING):
        resolved = resolve_engine_shared_volume_path()

    assert resolved == str(configured)
    assert "not on the same filesystem" not in caplog.text
