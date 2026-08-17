"""Tests for the local mounted-folder watch-source client
(``app/services/watch_sources/local_client.py``).

``LocalWatchClient`` only needs a ``WatchSource``-shaped object with a
``resolved_local_path`` property and a ``recursive`` flag -- no DB row is
required for that. Tests build a real, unsaved ``WatchSource`` ORM instance
(matching production usage: ``create_client`` in ``base.py`` is handed a real
row) and point ``settings.WATCH_FOLDER_PATH`` at a pytest ``tmp_path`` via
``monkeypatch``, so the traversal-guard logic in
``WatchSource.resolved_local_path`` and ``LocalWatchClient._is_within_root``
runs for real rather than being stubbed out.

``watch_settings_service.file_stability_seconds()`` opens its own short DB
session (see ``watch_settings_service._session``) -- a genuinely
out-of-process seam for a filesystem-listing unit test, so it is patched to a
fixed value rather than exercised against a live Postgres row.
"""

from __future__ import annotations

import os
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.models.watch_source import WatchSource
from app.services.watch_sources.local_client import LocalWatchClient

_STABILITY_SECONDS = "app.services.watch_settings_service.file_stability_seconds"


@pytest.fixture
def watch_root(tmp_path, monkeypatch):
    """Configure WATCH_FOLDER_PATH at tmp_path/mount and return the mount dir."""
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setattr(settings, "WATCH_FOLDER_PATH", str(mount))
    return mount


def _make_client(
    watch_root, local_path: str = "watched", recursive: bool = True
) -> LocalWatchClient:
    source = WatchSource(
        name="test-source",
        source_type="local",
        local_path=local_path,
        recursive=recursive,
        user_id=1,
    )
    return LocalWatchClient(source)


def _age_file(path, seconds_old: int) -> None:
    """Backdate a file's mtime so it clears the stability window."""
    when = time.time() - seconds_old
    os.utime(path, (when, when))


class TestTestConnection:
    def test_not_configured_when_watch_folder_path_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "WATCH_FOLDER_PATH", "")
        source = WatchSource(
            name="x", source_type="local", local_path="sub", recursive=True, user_id=1
        )
        client = LocalWatchClient(source)
        ok, message = client.test_connection()
        assert ok is False
        assert message == "Local watch folder is not configured on the server"

    def test_escaping_local_path_reports_the_value_error(self, watch_root):
        # ".." resolves outside WATCH_FOLDER_PATH -> WatchSource.resolved_local_path
        # raises ValueError, which test_connection surfaces as its message.
        client = _make_client(watch_root, local_path="../escape")
        ok, message = client.test_connection()
        assert ok is False
        assert "escapes watch root" in message

    def test_path_does_not_exist(self, watch_root):
        client = _make_client(watch_root, local_path="nosuchdir")
        ok, message = client.test_connection()
        assert ok is False
        assert message == f"Path does not exist: {watch_root / 'nosuchdir'}"

    def test_path_is_not_a_directory(self, watch_root):
        (watch_root / "afile.txt").write_text("hi")
        client = _make_client(watch_root, local_path="afile.txt")
        ok, message = client.test_connection()
        assert ok is False
        assert message == f"Path is not a directory: {watch_root / 'afile.txt'}"

    def test_path_not_readable(self, watch_root):
        target = watch_root / "locked"
        target.mkdir()
        os.chmod(target, 0o000)
        try:
            client = _make_client(watch_root, local_path="locked")
            ok, message = client.test_connection()
            assert ok is False
            assert message == f"Path is not readable: {target}"
        finally:
            os.chmod(target, 0o700)

    def test_ok_for_a_readable_directory(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        client = _make_client(watch_root, local_path="watched")
        ok, message = client.test_connection()
        assert ok is True
        assert message == f"OK — {target}"


class TestListFiles:
    def test_lists_stable_files_and_skips_fresh_ones(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        stable = target / "stable.mp3"
        stable.write_bytes(b"x" * 100)
        _age_file(stable, seconds_old=120)
        fresh = target / "fresh.mp3"
        fresh.write_bytes(b"y" * 50)
        # fresh.mp3 keeps its just-written mtime -> inside the stability window

        client = _make_client(watch_root, local_path="watched")
        with patch(_STABILITY_SECONDS, return_value=30):
            results = client.list_files()

        names = [r.name for r in results]
        assert names == ["stable.mp3"]
        assert results[0].size == 100
        assert results[0].path == str(stable)

    def test_extension_filter(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        for name in ("a.mp3", "b.wav", "c.txt"):
            f = target / name
            f.write_bytes(b"x")
            _age_file(f, seconds_old=120)

        client = _make_client(watch_root, local_path="watched")
        with patch(_STABILITY_SECONDS, return_value=30):
            results = client.list_files(extensions=[".mp3", ".wav"])

        names = sorted(r.name for r in results)
        assert names == ["a.mp3", "b.wav"]

    def test_non_recursive_skips_subdirectory_files(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        top = target / "top.mp3"
        top.write_bytes(b"x")
        _age_file(top, seconds_old=120)
        sub = target / "subdir"
        sub.mkdir()
        nested = sub / "nested.mp3"
        nested.write_bytes(b"x")
        _age_file(nested, seconds_old=120)

        client = _make_client(watch_root, local_path="watched", recursive=False)
        with patch(_STABILITY_SECONDS, return_value=30):
            results = client.list_files(recursive=False)

        assert [r.name for r in results] == ["top.mp3"]

    def test_recursive_includes_subdirectory_files(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        sub = target / "subdir"
        sub.mkdir()
        nested = sub / "nested.mp3"
        nested.write_bytes(b"x")
        _age_file(nested, seconds_old=120)

        client = _make_client(watch_root, local_path="watched", recursive=True)
        with patch(_STABILITY_SECONDS, return_value=30):
            results = client.list_files(recursive=True)

        assert [r.name for r in results] == ["nested.mp3"]

    def test_symlinked_file_is_skipped(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        real_elsewhere = watch_root / "real.mp3"
        real_elsewhere.write_bytes(b"x")
        _age_file(real_elsewhere, seconds_old=120)
        (target / "link.mp3").symlink_to(real_elsewhere)

        client = _make_client(watch_root, local_path="watched")
        with patch(_STABILITY_SECONDS, return_value=30):
            results = client.list_files()

        assert results == []

    def test_min_modified_filters_out_older_files(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        old = target / "old.mp3"
        old.write_bytes(b"x")
        _age_file(old, seconds_old=3600)

        client = _make_client(watch_root, local_path="watched")
        cutoff = datetime.now(UTC) - timedelta(minutes=10)
        with patch(_STABILITY_SECONDS, return_value=30):
            results = client.list_files(min_modified=cutoff)

        assert results == []

    def test_min_modified_keeps_newer_files(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        recent = target / "recent.mp3"
        recent.write_bytes(b"x")
        _age_file(recent, seconds_old=60)  # older than stability window, newer than cutoff

        client = _make_client(watch_root, local_path="watched")
        cutoff = datetime.now(UTC) - timedelta(minutes=10)
        with patch(_STABILITY_SECONDS, return_value=30):
            results = client.list_files(min_modified=cutoff)

        assert [r.name for r in results] == ["recent.mp3"]

    def test_no_files_returns_empty_list(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        client = _make_client(watch_root, local_path="watched")
        with patch(_STABILITY_SECONDS, return_value=30):
            assert client.list_files() == []


class TestDownloadFile:
    def test_returns_size_for_a_path_within_root(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        f = target / "song.mp3"
        f.write_bytes(b"x" * 42)

        client = _make_client(watch_root, local_path="watched")
        size = client.download_file(str(f), "/unused/local/path.mp3")
        assert size == 42

    def test_raises_for_a_path_outside_root(self, watch_root, tmp_path):
        target = watch_root / "watched"
        target.mkdir()
        outside = tmp_path / "outside.mp3"
        outside.write_bytes(b"x")

        client = _make_client(watch_root, local_path="watched")
        with pytest.raises(ValueError, match="outside watch root"):
            client.download_file(str(outside), "/unused/local/path.mp3")


class TestUploadFile:
    def test_writes_into_an_existing_directory_within_root(self, watch_root, tmp_path):
        target = watch_root / "watched"
        target.mkdir()
        src = tmp_path / "stitched.mp4"
        src.write_bytes(b"content")

        client = _make_client(watch_root, local_path="watched")
        dest = target / "output.mp4"
        assert client.upload_file(str(src), str(dest)) is True
        assert dest.read_bytes() == b"content"

    def test_creates_a_new_subdirectory_within_root(self, watch_root, tmp_path):
        target = watch_root / "watched"
        target.mkdir()
        src = tmp_path / "stitched.mp4"
        src.write_bytes(b"content")

        client = _make_client(watch_root, local_path="watched")
        dest = target / "newsub" / "output.mp4"
        assert not dest.parent.exists()
        assert client.upload_file(str(src), str(dest)) is True
        assert dest.read_bytes() == b"content"

    def test_raises_for_a_destination_outside_root_with_an_existing_parent(
        self, watch_root, tmp_path
    ):
        target = watch_root / "watched"
        target.mkdir()
        src = tmp_path / "stitched.mp4"
        src.write_bytes(b"content")
        outside = tmp_path / "outside"
        outside.mkdir()

        client = _make_client(watch_root, local_path="watched")
        with pytest.raises(ValueError, match="outside watch root"):
            client.upload_file(str(src), str(outside / "evil.mp4"))

    def test_raises_for_a_destination_outside_root_with_a_missing_parent(
        self, watch_root, tmp_path
    ):
        """⚠️ Suspected bug (see final report): the traversal guard is bypassed
        when the destination's parent directory does not yet exist. The code
        falls back to checking ``self.root`` against ``self.root`` (trivially
        True) instead of validating the actual destination, so
        ``dest.parent.mkdir(parents=True)`` + ``shutil.copy2`` proceed and
        write OUTSIDE the watch root. This test asserts the correct/expected
        behaviour (raise ValueError) and is therefore currently RED.
        """
        target = watch_root / "watched"
        target.mkdir()
        src = tmp_path / "stitched.mp4"
        src.write_bytes(b"content")
        outside_missing = tmp_path / "outside_missing" / "nested"
        assert not outside_missing.exists()

        client = _make_client(watch_root, local_path="watched")
        with pytest.raises(ValueError, match="outside watch root"):
            client.upload_file(str(src), str(outside_missing / "evil.mp4"))

        # The write must never have happened.
        assert not (outside_missing / "evil.mp4").exists()


class TestDeleteFile:
    def test_deletes_a_file_within_root(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        f = target / "gone.mp3"
        f.write_bytes(b"x")

        client = _make_client(watch_root, local_path="watched")
        assert client.delete_file(str(f)) is True
        assert not f.exists()

    def test_missing_file_is_still_true(self, watch_root):
        target = watch_root / "watched"
        target.mkdir()
        missing = target / "nope.mp3"

        client = _make_client(watch_root, local_path="watched")
        assert client.delete_file(str(missing)) is True

    def test_raises_for_a_path_outside_root(self, watch_root, tmp_path):
        target = watch_root / "watched"
        target.mkdir()
        outside = tmp_path / "outside.mp3"
        outside.write_bytes(b"x")

        client = _make_client(watch_root, local_path="watched")
        with pytest.raises(ValueError, match="outside watch root"):
            client.delete_file(str(outside))

    def test_oserror_on_delete_returns_false(self, watch_root):
        # unlink() on a directory raises IsADirectoryError (an OSError subclass),
        # exercising the except-and-return-False branch without needing chmod.
        target = watch_root / "watched"
        target.mkdir()
        a_directory = target / "im_a_dir"
        a_directory.mkdir()

        client = _make_client(watch_root, local_path="watched")
        assert client.delete_file(str(a_directory)) is False
        assert a_directory.exists()
