"""Tests for ``app/utils/temp_file_utils.py`` (issue #474).

Pure filesystem I/O helper with zero prior test coverage. These tests run against
the real filesystem (via ``tmp_path``/``monkeypatch.setattr(tempfile, "tempdir", ...)``)
rather than mocking ``os``/``pathlib``, since the whole point of this module is that the
I/O actually happens and actually gets cleaned up.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile

import pytest

from app.utils.temp_file_utils import cleanup_temp_file
from app.utils.temp_file_utils import download_to_temp_file
from app.utils.temp_file_utils import temp_file_context

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _redirect_tempdir(tmp_path, monkeypatch):
    """Point ``tempfile.mkstemp`` (no explicit dir=) at a throwaway pytest dir.

    ``tempfile.tempdir`` is the documented module attribute ``gettempdir()``
    consults before falling back to the OS default; setting it directly (rather
    than the ``TMPDIR`` env var) is reliable even if the module already cached a
    default in this process.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    yield


# =============================================================================
# download_to_temp_file
# =============================================================================
def test_download_to_temp_file_writes_content_and_returns_path(tmp_path):
    data = io.BytesIO(b"hello world")

    path = download_to_temp_file(data)

    assert os.path.exists(path)
    assert os.path.dirname(path) == str(tmp_path)
    with open(path, "rb") as f:
        assert f.read() == b"hello world"

    os.unlink(path)


def test_download_to_temp_file_respects_suffix():
    data = io.BytesIO(b"audio-bytes")

    path = download_to_temp_file(data, suffix=".wav")

    assert path.endswith(".wav")
    os.unlink(path)


def test_download_to_temp_file_writes_empty_content():
    data = io.BytesIO(b"")

    path = download_to_temp_file(data)

    assert os.path.exists(path)
    assert os.path.getsize(path) == 0
    os.unlink(path)


def test_download_to_temp_file_cleans_up_and_reraises_on_read_error():
    class ExplodingReader:
        def read(self):
            raise OSError("simulated read failure")

    created_paths = []
    real_mkstemp = tempfile.mkstemp

    def _tracking_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created_paths.append(path)
        return fd, path

    import app.utils.temp_file_utils as mod

    # Only tempfile.mkstemp is intercepted (to observe which path was created) —
    # the write, close, and cleanup logic all run for real.
    orig = mod.tempfile.mkstemp
    mod.tempfile.mkstemp = _tracking_mkstemp
    try:
        with pytest.raises(OSError, match="simulated read failure"):
            # ExplodingReader only implements the subset of BinaryIO the function
            # actually calls (.read()) — a minimal duck-typed fake, not a real one.
            download_to_temp_file(ExplodingReader())  # type: ignore[arg-type]
    finally:
        mod.tempfile.mkstemp = orig

    assert len(created_paths) == 1
    assert not os.path.exists(created_paths[0]), "temp file must be removed after the error"


# =============================================================================
# cleanup_temp_file
# =============================================================================
def test_cleanup_temp_file_removes_an_existing_file(tmp_path):
    f = tmp_path / "gone.txt"
    f.write_text("bye")

    cleanup_temp_file(str(f))

    assert not f.exists()


def test_cleanup_temp_file_is_a_noop_for_none(caplog):
    with caplog.at_level(logging.WARNING, logger="app.utils.temp_file_utils"):
        cleanup_temp_file(None)  # must not raise

    # A real no-op, not a silently-swallowed attempt: nothing gets logged either.
    assert caplog.records == []


def test_cleanup_temp_file_is_a_noop_for_a_nonexistent_path(tmp_path):
    missing = tmp_path / "does-not-exist.txt"

    cleanup_temp_file(str(missing))  # must not raise

    assert not missing.exists()


def test_cleanup_temp_file_logs_a_warning_when_unlink_fails(tmp_path, monkeypatch, caplog):
    f = tmp_path / "locked.txt"
    f.write_text("data")

    def _boom(path):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "unlink", _boom)

    with caplog.at_level(logging.WARNING, logger="app.utils.temp_file_utils"):
        cleanup_temp_file(str(f))  # must not raise despite the OSError

    assert f.exists(), "file is left in place when unlink fails"
    assert any("Failed to clean up temp file" in r.message for r in caplog.records)


# =============================================================================
# temp_file_context
# =============================================================================
def test_temp_file_context_yields_a_readable_path_and_cleans_up_after():
    data = io.BytesIO(b"context data")

    with temp_file_context(data) as path:
        assert os.path.exists(path)
        with open(path, "rb") as f:
            assert f.read() == b"context data"
        captured_path = path

    assert not os.path.exists(captured_path)


def test_temp_file_context_cleans_up_even_when_the_body_raises():
    data = io.BytesIO(b"will be cleaned")
    captured_path = None

    with pytest.raises(ValueError, match="boom"):
        with temp_file_context(data) as path:
            captured_path = path
            assert os.path.exists(path)
            raise ValueError("boom")

    assert captured_path is not None
    assert not os.path.exists(captured_path)


def test_temp_file_context_passes_through_suffix():
    data = io.BytesIO(b"x")

    with temp_file_context(data, suffix=".mp3") as path:
        assert path.endswith(".mp3")
