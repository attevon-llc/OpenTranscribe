"""Unit tests for SMB watch-source download integrity (issue #293).

`SMBWatchClient.download_file` verifies the written byte count against the remote
`stat()` size, but the `raise` used to sit *inside* the `try` whose `except Exception`
only logged at debug — so the one condition the check exists to catch was the one it
could not report. A truncated download was returned as a success, uploaded to MinIO,
given a `MediaFile` row, and transcribed.

`smbclient` is imported lazily inside the methods, so these tests inject a fake module
into `sys.modules` rather than requiring smbprotocol or a live share.
"""

from __future__ import annotations

import io
import sys
import types

import pytest

from app.services.watch_sources.smb_client import SMBWatchClient

REMOTE = "\\\\server\\share\\clip.mp4"


def _install_fake_smbclient(monkeypatch, *, payload: bytes, stat_size, stat_raises=False):
    """Install a fake `smbclient` module returning *payload* bytes and *stat_size*."""
    fake = types.ModuleType("smbclient")

    def open_file(path, mode="rb", **kwargs):
        return io.BytesIO(payload)

    def stat(path):
        if stat_raises:
            raise OSError("stat unsupported on this server")
        return types.SimpleNamespace(st_size=stat_size, st_mtime=0)

    fake.open_file = open_file  # type: ignore[attr-defined]
    fake.stat = stat  # type: ignore[attr-defined]
    fake.register_session = lambda *a, **k: None  # type: ignore[attr-defined]
    fake.ClientConfig = lambda *a, **k: None  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "smbclient", fake)
    return fake


def _client() -> SMBWatchClient:
    client = SMBWatchClient(server="server", share="share", path="/", username="u", password="p")
    client._registered = True  # skip session registration
    return client


def test_truncated_download_raises(monkeypatch, tmp_path):
    """The whole point of #293: a short read must fail loudly, not import silently."""
    payload = b"x" * 500
    _install_fake_smbclient(monkeypatch, payload=payload, stat_size=2000)
    dest = tmp_path / "clip.mp4"

    with pytest.raises(RuntimeError, match="size mismatch"):
        _client().download_file(REMOTE, str(dest))


def test_complete_download_returns_byte_count(monkeypatch, tmp_path):
    payload = b"y" * 4096
    _install_fake_smbclient(monkeypatch, payload=payload, stat_size=len(payload))
    dest = tmp_path / "clip.mp4"

    assert _client().download_file(REMOTE, str(dest)) == len(payload)
    assert dest.read_bytes() == payload


def test_oversized_download_also_raises(monkeypatch, tmp_path):
    """Mismatch in either direction is a mismatch."""
    payload = b"z" * 3000
    _install_fake_smbclient(monkeypatch, payload=payload, stat_size=1500)
    dest = tmp_path / "clip.mp4"

    with pytest.raises(RuntimeError, match="size mismatch"):
        _client().download_file(REMOTE, str(dest))


def test_unavailable_stat_skips_verification_with_warning(monkeypatch, tmp_path, caplog):
    """Servers without stat() must still import — that is why the skip handler exists."""
    payload = b"w" * 1024
    _install_fake_smbclient(monkeypatch, payload=payload, stat_size=None, stat_raises=True)
    dest = tmp_path / "clip.mp4"

    with caplog.at_level("WARNING"):
        assert _client().download_file(REMOTE, str(dest)) == len(payload)

    assert any("size verification skipped" in r.message for r in caplog.records), (
        "skipping integrity verification must be logged above debug level"
    )


def test_chunked_read_accumulates_full_payload(monkeypatch, tmp_path):
    """Payload larger than the 64 KiB chunk size must be written and counted in full."""
    payload = b"q" * (64 * 1024 * 3 + 17)
    _install_fake_smbclient(monkeypatch, payload=payload, stat_size=len(payload))
    dest = tmp_path / "big.mp4"

    assert _client().download_file(REMOTE, str(dest)) == len(payload)
    assert dest.stat().st_size == len(payload)
