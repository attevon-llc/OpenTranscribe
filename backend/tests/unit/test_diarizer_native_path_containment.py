"""Issue #661 phase 1.3 — real path containment for the reuse-WAV shared-volume check.

``str.startswith`` against ``_ENGINE_SHARED_PREFIX`` is not containment: no ``..`` resolution,
no symlink resolution, and a sibling directory sharing the string prefix (e.g.
``/scratch/opentranscribe-evil``) passes it. It is also keyed on the wrong thing after E2:
the boundary must be the pipeline_scratch VOLUME ROOT, not the ``engine/`` writer subdir, or
rediarize's ``<uuid>/audio.wav`` path is wrongly rejected.
"""

from __future__ import annotations

import os

import pytest

from app.transcription import diarizer_native


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    root = tmp_path / "opentranscribe"
    root.mkdir()
    monkeypatch.setattr(diarizer_native, "_SHARED_VOLUME_ROOT", os.path.realpath(str(root)))
    return root


def test_engine_namespace_path_is_contained(_root):
    p = _root / "engine" / "x.wav"
    p.parent.mkdir()
    p.write_bytes(b"x")
    assert diarizer_native._path_is_on_shared_volume(str(p)) is True


def test_uuid_namespace_path_is_contained(_root):
    p = _root / "11111111-1111-1111-1111-111111111111" / "audio.wav"
    p.parent.mkdir()
    p.write_bytes(b"x")
    assert diarizer_native._path_is_on_shared_volume(str(p)) is True


def test_sibling_directory_with_shared_string_prefix_is_rejected(_root, tmp_path):
    evil = tmp_path / "opentranscribe-evil" / "x.wav"
    evil.parent.mkdir()
    evil.write_bytes(b"x")
    assert diarizer_native._path_is_on_shared_volume(str(evil)) is False


def test_dotdot_traversal_out_of_root_is_rejected(_root):
    escaped = str(_root / "engine" / ".." / ".." / "etc" / "passwd")
    assert diarizer_native._path_is_on_shared_volume(escaped) is False


def test_symlink_escaping_root_is_rejected(_root, tmp_path):
    outside = tmp_path / "outside_target"
    outside.mkdir()
    link = _root / "escape_link"
    link.symlink_to(outside)
    assert diarizer_native._path_is_on_shared_volume(str(link / "x.wav")) is False
