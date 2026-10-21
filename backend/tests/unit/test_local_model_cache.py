"""The hub-cache scan: a model is downloaded only when it is complete (#1246).

Discovery used to call a repository downloaded when its ``blobs/`` directory was non-empty, so a
half-finished download (only ``*.incomplete`` blobs) offered Activate on a model that cannot load.
"""

from __future__ import annotations

from fixtures.hf_cache import REVISION
from fixtures.hf_cache import make_complete_repo
from fixtures.hf_cache import make_partial_repo

from app.services.asr import model_discovery
from app.services.local_models import cache

TINY = "Systran/faster-whisper-tiny"
BASE = "Systran/faster-whisper-base"


def test_a_complete_repo_is_complete(hf_cache):
    make_complete_repo(hf_cache, TINY)
    assert cache.is_complete(TINY) is True
    assert cache.has_partial(TINY) is False


def test_blobs_only_is_not_complete(hf_cache):
    root = make_complete_repo(hf_cache, TINY)
    (root / "refs" / "main").unlink()
    assert cache.is_complete(TINY) is False


def test_an_incomplete_blob_makes_a_repo_incomplete_even_with_a_snapshot(hf_cache):
    root = make_complete_repo(hf_cache, TINY)
    (root / "blobs" / "etag-other.incomplete").write_bytes(b"x")
    assert cache.has_partial(TINY) is True
    assert cache.is_complete(TINY) is False


def test_refs_without_a_snapshot_is_not_complete(hf_cache):
    root = make_complete_repo(hf_cache, TINY)
    (root / "snapshots" / REVISION / "model.bin").unlink()
    assert cache.is_complete(TINY) is False


def test_a_dangling_snapshot_symlink_is_not_complete(hf_cache):
    root = make_complete_repo(hf_cache, TINY)
    (root / "blobs" / "etag-model.bin").unlink()
    assert cache.is_complete(TINY) is False


def test_a_traversing_ref_is_refused(hf_cache):
    root = make_complete_repo(hf_cache, TINY)
    (root / "refs" / "main").write_text("../../../elsewhere", encoding="utf-8")
    assert cache.is_complete(TINY) is False


def test_missing_repo_is_not_complete(hf_cache):
    assert cache.is_complete(TINY) is False
    assert cache.present_bytes(TINY) == 0


def test_present_bytes_counts_only_complete_blobs(hf_cache):
    root = make_complete_repo(hf_cache, TINY, model_bytes=1000)
    (root / "blobs" / "etag-x.incomplete").write_bytes(b"x" * 5000)
    assert cache.present_bytes(TINY) == 1000 + 64


def test_remove_incomplete_deletes_only_this_repos_incomplete_blobs(hf_cache):
    mine = make_partial_repo(hf_cache, TINY)
    (mine / "blobs" / "etag-done").write_bytes(b"complete")
    neighbour = make_partial_repo(hf_cache, BASE)

    removed = cache.remove_incomplete(TINY)

    assert removed == 1
    assert not (mine / "blobs" / "etag-model.bin.incomplete").exists()
    assert (mine / "blobs" / "etag-done").exists()
    assert (neighbour / "blobs" / "etag-model.bin.incomplete").exists()


def test_remove_incomplete_can_be_limited_to_named_blobs(hf_cache):
    root = make_partial_repo(hf_cache, TINY)
    (root / "blobs" / "other.incomplete").write_bytes(b"x")

    assert cache.remove_incomplete(TINY, ["etag-model.bin"]) == 1
    assert (root / "blobs" / "other.incomplete").exists()


def test_cache_writable_and_free_bytes(hf_cache):
    assert cache.cache_writable() is True
    assert cache.free_bytes() > 0


def test_discovery_does_not_report_a_partial_download_as_downloaded(hf_cache):
    make_partial_repo(hf_cache, TINY)
    make_complete_repo(hf_cache, BASE)

    names = [m["short_name"] for m in model_discovery.discover_local_models()]

    assert names == ["base"]


def test_discovery_reads_the_same_directory_the_downloader_uses(hf_cache):
    make_complete_repo(hf_cache, TINY)
    assert cache.hub_cache_dir() == hf_cache
    assert [m["short_name"] for m in model_discovery.discover_local_models()] == ["tiny"]


def test_discovery_ignores_the_host_path_model_cache_dir(hf_cache, monkeypatch, tmp_path):
    """MODEL_CACHE_DIR is a HOST path that leaks into containers via env_file."""
    decoy = tmp_path / "decoy"
    make_complete_repo(decoy / "huggingface" / "hub", TINY)
    monkeypatch.setenv("MODEL_CACHE_DIR", str(decoy))
    assert model_discovery.discover_local_models() == []
