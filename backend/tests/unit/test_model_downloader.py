"""Tests for ``app/services/search/model_downloader.py`` (issue #474).

Startup-time OpenSearch neural model fetcher: registry lookup, cache-hit
short-circuit, the download+verify+manifest sequence, and
``check_internet_connectivity``. The only genuinely out-of-process seam is
the network (``urllib.request.urlretrieve`` / ``urlopen``) — those are
mocked. Everything else (registry lookup, cache-dir/manifest filesystem
state) runs against a real ``tmp_path`` directory so the actual logic is
exercised, not a stand-in for it.
"""

from __future__ import annotations

import json
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from app.services.search import model_downloader
from app.services.search.model_downloader import _OPENSEARCH_MODEL_REGISTRY
from app.services.search.model_downloader import check_internet_connectivity
from app.services.search.model_downloader import ensure_model_downloaded

_KNOWN_MODEL = "huggingface/sentence-transformers/all-MiniLM-L6-v2"
_KNOWN_MODEL_INFO = _OPENSEARCH_MODEL_REGISTRY[_KNOWN_MODEL]


def _fake_urlretrieve_writes_bytes(payload: bytes = b"fake-model-zip-bytes"):
    """Return a stand-in for ``urlretrieve`` that actually writes the destination file,
    the way a real download would — so downstream size/existence checks are real."""

    def _fake(url, filename, reporthook=None):
        Path(filename).write_bytes(payload)
        if reporthook is not None:
            reporthook(0, len(payload), len(payload))
            reporthook(50, len(payload), len(payload))

    return _fake


# =============================================================================
# ensure_model_downloaded — registry lookup
# =============================================================================


def test_ensure_model_downloaded_unknown_model_returns_none(tmp_path):
    result = ensure_model_downloaded("not/a/real-model", cache_dir=tmp_path)

    assert result is None
    # Nothing was created for an unknown model.
    assert list(tmp_path.iterdir()) == []


# =============================================================================
# ensure_model_downloaded — cache hit short-circuits the download
# =============================================================================


def test_ensure_model_downloaded_already_cached_returns_path_without_downloading(tmp_path):
    short_name = _KNOWN_MODEL_INFO["short_name"]
    filename = _KNOWN_MODEL_INFO["filename"]
    model_dir = tmp_path / short_name
    model_dir.mkdir(parents=True)
    cached_path = model_dir / filename
    cached_path.write_bytes(b"already-here")

    with patch("app.services.search.model_downloader.urllib.request.urlretrieve") as retrieve:
        result = ensure_model_downloaded(_KNOWN_MODEL, cache_dir=tmp_path)

    assert result == cached_path
    retrieve.assert_not_called()
    # The cached bytes must not have been touched.
    assert cached_path.read_bytes() == b"already-here"


def test_ensure_model_downloaded_zero_byte_cached_file_is_treated_as_not_cached(tmp_path):
    """A 0-byte file at the destination (e.g. a prior failed/interrupted download)
    must NOT satisfy the cache-hit check — ``model_path.stat().st_size > 0`` is the
    guard against exactly this."""
    short_name = _KNOWN_MODEL_INFO["short_name"]
    filename = _KNOWN_MODEL_INFO["filename"]
    model_dir = tmp_path / short_name
    model_dir.mkdir(parents=True)
    stale_empty = model_dir / filename
    stale_empty.write_bytes(b"")

    with patch(
        "app.services.search.model_downloader.urllib.request.urlretrieve",
        side_effect=_fake_urlretrieve_writes_bytes(),
    ) as retrieve:
        result = ensure_model_downloaded(_KNOWN_MODEL, cache_dir=tmp_path)

    retrieve.assert_called_once()
    assert result == stale_empty
    assert stale_empty.read_bytes() == b"fake-model-zip-bytes"


# =============================================================================
# ensure_model_downloaded — successful download path
# =============================================================================


def test_ensure_model_downloaded_success_writes_file_and_manifest(tmp_path):
    with patch(
        "app.services.search.model_downloader.urllib.request.urlretrieve",
        side_effect=_fake_urlretrieve_writes_bytes(b"real-bytes-here"),
    ):
        result = ensure_model_downloaded(_KNOWN_MODEL, cache_dir=tmp_path)

    short_name = _KNOWN_MODEL_INFO["short_name"]
    filename = _KNOWN_MODEL_INFO["filename"]
    expected_path = tmp_path / short_name / filename
    assert result == expected_path
    assert expected_path.read_bytes() == b"real-bytes-here"

    manifest_path = tmp_path / "model_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["models"] == [
        {
            "name": _KNOWN_MODEL,
            "short_name": short_name,
            "version": _KNOWN_MODEL_INFO["version"],
            "dimension": _KNOWN_MODEL_INFO["dimension"],
            "downloaded_at": manifest["models"][0]["downloaded_at"],
        }
    ]
    assert "updated_at" in manifest


def test_ensure_model_downloaded_url_uses_registry_url_base_name_version_and_filename(tmp_path):
    captured_urls = []

    def _fake(url, filename, reporthook=None):
        captured_urls.append(url)
        Path(filename).write_bytes(b"x")

    with patch(
        "app.services.search.model_downloader.urllib.request.urlretrieve", side_effect=_fake
    ):
        ensure_model_downloaded(_KNOWN_MODEL, cache_dir=tmp_path)

    assert len(captured_urls) == 1
    expected = (
        f"{_KNOWN_MODEL_INFO['url_base']}/{_KNOWN_MODEL}/{_KNOWN_MODEL_INFO['version']}"
        f"/torch_script/{_KNOWN_MODEL_INFO['filename']}"
    )
    assert captured_urls[0] == expected


# =============================================================================
# ensure_model_downloaded — download produced an empty file
# =============================================================================


def test_ensure_model_downloaded_writes_empty_file_returns_none(tmp_path):
    """urlretrieve 'succeeded' (raised nothing) but the destination file is empty --
    the post-download verify step must reject that, not just the pre-check."""

    def _fake_empty(url, filename, reporthook=None):
        Path(filename).write_bytes(b"")

    with patch(
        "app.services.search.model_downloader.urllib.request.urlretrieve",
        side_effect=_fake_empty,
    ):
        result = ensure_model_downloaded(_KNOWN_MODEL, cache_dir=tmp_path)

    assert result is None
    # No manifest should be written for a failed download.
    assert not (tmp_path / "model_manifest.json").exists()


def test_ensure_model_downloaded_file_never_created_returns_none(tmp_path):
    def _fake_noop(url, filename, reporthook=None):
        pass  # simulate a urlretrieve that "succeeds" without writing anything

    with patch(
        "app.services.search.model_downloader.urllib.request.urlretrieve",
        side_effect=_fake_noop,
    ):
        result = ensure_model_downloaded(_KNOWN_MODEL, cache_dir=tmp_path)

    assert result is None


# =============================================================================
# ensure_model_downloaded — exception handling
# =============================================================================


def test_ensure_model_downloaded_http_error_returns_none(tmp_path):
    with patch(
        "app.services.search.model_downloader.urllib.request.urlretrieve",
        side_effect=urllib.error.HTTPError(
            url="http://example.test", code=404, msg="Not Found", hdrs=Message(), fp=None
        ),
    ):
        result = ensure_model_downloaded(_KNOWN_MODEL, cache_dir=tmp_path)

    assert result is None


def test_ensure_model_downloaded_url_error_returns_none(tmp_path):
    with patch(
        "app.services.search.model_downloader.urllib.request.urlretrieve",
        side_effect=urllib.error.URLError("network unreachable"),
    ):
        result = ensure_model_downloaded(_KNOWN_MODEL, cache_dir=tmp_path)

    assert result is None


def test_ensure_model_downloaded_generic_exception_returns_none(tmp_path):
    with patch(
        "app.services.search.model_downloader.urllib.request.urlretrieve",
        side_effect=OSError("disk full"),
    ):
        result = ensure_model_downloaded(_KNOWN_MODEL, cache_dir=tmp_path)

    assert result is None


# =============================================================================
# ensure_model_downloaded — default cache dir
# =============================================================================


def test_ensure_model_downloaded_defaults_to_home_cache_dir_when_none_given(tmp_path):
    fake_home_cache = tmp_path / "home-cache"
    with (
        patch("app.services.search.model_downloader._DEFAULT_CACHE_DIR", fake_home_cache),
        patch(
            "app.services.search.model_downloader.urllib.request.urlretrieve",
            side_effect=_fake_urlretrieve_writes_bytes(),
        ),
    ):
        result = ensure_model_downloaded(_KNOWN_MODEL, cache_dir=None)

    assert result is not None
    assert result.is_relative_to(fake_home_cache)


# =============================================================================
# _update_manifest
# =============================================================================


def test_update_manifest_creates_new_manifest_with_one_entry(tmp_path):
    model_downloader._update_manifest(tmp_path, _KNOWN_MODEL, _KNOWN_MODEL_INFO)

    manifest = json.loads((tmp_path / "model_manifest.json").read_text())
    assert len(manifest["models"]) == 1
    assert manifest["models"][0]["name"] == _KNOWN_MODEL
    assert manifest["models"][0]["dimension"] == _KNOWN_MODEL_INFO["dimension"]


def test_update_manifest_replaces_existing_entry_for_the_same_model(tmp_path):
    model_downloader._update_manifest(tmp_path, _KNOWN_MODEL, _KNOWN_MODEL_INFO)

    other_model = "huggingface/sentence-transformers/all-mpnet-base-v2"
    other_info = _OPENSEARCH_MODEL_REGISTRY[other_model]
    model_downloader._update_manifest(tmp_path, other_model, other_info)

    # Re-update the FIRST model — must replace, not duplicate, its entry.
    model_downloader._update_manifest(tmp_path, _KNOWN_MODEL, _KNOWN_MODEL_INFO)

    manifest = json.loads((tmp_path / "model_manifest.json").read_text())
    names = [m["name"] for m in manifest["models"]]
    assert sorted(names) == sorted([_KNOWN_MODEL, other_model])
    assert names.count(_KNOWN_MODEL) == 1


def test_update_manifest_survives_corrupt_existing_manifest(tmp_path):
    manifest_path = tmp_path / "model_manifest.json"
    manifest_path.write_text("{ this is not valid json ]]]")

    model_downloader._update_manifest(tmp_path, _KNOWN_MODEL, _KNOWN_MODEL_INFO)

    manifest = json.loads(manifest_path.read_text())
    assert len(manifest["models"]) == 1
    assert manifest["models"][0]["name"] == _KNOWN_MODEL


# =============================================================================
# check_internet_connectivity
# =============================================================================


def test_check_internet_connectivity_true_when_urlopen_succeeds():
    with patch("app.services.search.model_downloader.urllib.request.urlopen") as urlopen:
        urlopen.return_value = object()
        assert check_internet_connectivity() is True


def test_check_internet_connectivity_false_on_url_error():
    with patch(
        "app.services.search.model_downloader.urllib.request.urlopen",
        side_effect=urllib.error.URLError("offline"),
    ):
        assert check_internet_connectivity() is False


def test_check_internet_connectivity_false_on_timeout():
    with patch(
        "app.services.search.model_downloader.urllib.request.urlopen",
        side_effect=TimeoutError("timed out"),
    ):
        assert check_internet_connectivity(timeout=0.5) is False
