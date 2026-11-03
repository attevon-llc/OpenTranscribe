"""The NLTK group of download-models.py must survive a hardlinked cache (v0.6.0 fix).

nltk 3.10.4's pathsec refuses any file with ``st_nlink > 1`` (CWE-59), and ``nltk.download``
reads the already-installed package zip before deciding whether to fetch. A model cache
seeded with ``cp -al`` therefore failed all six packages of the group, each with
``Security Violation [pathsec.open]: refusing multiply-linked file``, and the run exited 1.

These run the REAL ``nltk.download`` (a real ``nltk.downloader.Downloader``) over a real
cache whose files are given a second name with ``os.link``; the only substitution is the
package server, a ``file://`` index in ``tmp_path`` instead of raw.githubusercontent.com.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import zipfile
from pathlib import Path

import pytest

nltk = pytest.importorskip("nltk")
Downloader = pytest.importorskip("nltk.downloader").Downloader

REPO_ROOT = Path(__file__).resolve().parents[3]
DOWNLOADER_PY = REPO_ROOT / "scripts" / "download-models.py"

#: The six packages ``download_nltk_data`` fetches, and the subdir nltk installs each into.
PACKAGES = {
    "punkt_tab": "tokenizers",
    "punkt": "tokenizers",
    "averaged_perceptron_tagger_eng": "taggers",
    "maxent_ne_chunker_tab": "chunkers",
    "words": "corpora",
    "stopwords": "corpora",
}


@pytest.fixture
def downloader():
    """``download-models.py`` loaded as a module, with torch.load restored afterwards."""
    torch = pytest.importorskip("torch")
    saved_load, saved_empty_cache = torch.load, torch.mps.empty_cache
    spec = importlib.util.spec_from_file_location("download_models_nltk_links", DOWNLOADER_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        torch.load, torch.mps.empty_cache = saved_load, saved_empty_cache


def _local_package_server(server: Path) -> str:
    """Write one tiny real package zip per id plus an nltk ``index.xml``; return its URL."""
    server.mkdir()
    entries = []
    for pid, subdir in PACKAGES.items():
        archive = server / f"{pid}.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(f"{pid}/README", f"{pid} fixture\n")
        data = archive.read_bytes()
        entries.append(
            f'<package id="{pid}" name="{pid}" subdir="{subdir}" unzip="1" '
            f'size="{len(data)}" unzipped_size="{len(pid) + 9}" '
            f'checksum="{hashlib.md5(data, usedforsecurity=False).hexdigest()}" '
            f'sha256_checksum="{hashlib.sha256(data).hexdigest()}" url="{archive.as_uri()}" />'
        )
    index = server / "index.xml"
    index.write_text(
        '<?xml version="1.0"?>\n<nltk_data><packages>'
        + "".join(entries)
        + "</packages><collections></collections></nltk_data>"
    )
    return index.as_uri()


@pytest.fixture
def hardlinked_cache(tmp_path, monkeypatch):
    """An installed nltk_data whose every file has a second name, as ``cp -al`` leaves it.

    Returns ``(data_dir, twin_dir)``. ``nltk.download`` is a real Downloader pointed at the
    local server; the server dir is put on ``nltk.data.path`` because pathsec only serves
    ``file://`` URLs under an allowed root.
    """
    server = tmp_path / "server"
    real_download = Downloader(server_index_url=_local_package_server(server)).download
    monkeypatch.setattr(nltk, "download", real_download)
    monkeypatch.setattr(nltk.data, "path", [str(server), *nltk.data.path])

    data_dir = tmp_path / "nltk_data"
    data_dir.mkdir(mode=0o700)
    for pid in PACKAGES:
        assert real_download(pid, download_dir=str(data_dir), quiet=True), pid

    twin_dir = tmp_path / "seed_twin"
    for path in sorted(data_dir.rglob("*")):
        twin = twin_dir / path.relative_to(data_dir)
        if path.is_dir():
            twin.mkdir(parents=True)
        else:
            os.link(path, twin)
    monkeypatch.setenv("NLTK_DATA", str(data_dir))
    return data_dir, twin_dir


def _linked(root: Path) -> list[str]:
    return sorted(str(p) for p in root.rglob("*") if p.is_file() and p.stat().st_nlink > 1)


def test_premise_pathsec_refuses_the_hardlinked_cache(hardlinked_cache):
    """Without the fix the real nltk downloader refuses the cache, naming pathsec."""
    data_dir, _ = hardlinked_cache
    assert len(_linked(data_dir)) == 2 * len(PACKAGES), "a zip and a README per package"

    with pytest.raises(PermissionError, match="refusing multiply-linked file"):
        nltk.download("stopwords", download_dir=str(data_dir), quiet=True, raise_on_error=True)


def test_nltk_group_succeeds_on_a_hardlinked_cache(downloader, hardlinked_cache):
    data_dir, twin_dir = hardlinked_cache
    twin_before = {p: p.read_bytes() for p in twin_dir.rglob("*") if p.is_file()}

    result = downloader.download_nltk_data()["nltk"]

    assert result["status"] == "downloaded", result
    assert result["packages"] == list(PACKAGES)
    assert _linked(data_dir) == [], "every cache file must now have its own inode"
    for twin, content in twin_before.items():
        own = data_dir / twin.relative_to(twin_dir)
        assert own.read_bytes() == content, f"content changed: {own}"
        assert twin.read_bytes() == content, f"the other name was modified: {twin}"
    nltk.data.find("corpora/stopwords/README", paths=[str(data_dir)])


def test_unrewritable_hardlinks_fail_once_with_the_remedy(downloader, hardlinked_cache, capsys):
    """A read-only directory cannot be fixed in place: one clear failure, not six."""
    data_dir, _ = hardlinked_cache
    corpora = data_dir / "corpora"
    corpora.chmod(0o500)
    try:
        if os.access(corpora, os.W_OK):
            pytest.skip("running as root: a read-only directory cannot be simulated")
        result = downloader.download_nltk_data()["nltk"]
    finally:
        corpora.chmod(0o700)

    out = capsys.readouterr().out
    assert result["status"] == "failed"
    assert "multiply-linked" in result["error"], result
    assert "-links +1" in out, "the operator must be told the host command that fixes it"
    assert "Downloading NLTK package" not in out, "no package may be attempted on a bad cache"


def test_a_preplanted_symlink_at_a_temp_name_is_never_written_through(downloader, tmp_path):
    """The old fixed ``<file>.dehl`` temp name let a planted symlink redirect the copy."""
    root = tmp_path / "nltk_data" / "corpora"
    root.mkdir(parents=True)
    victim = tmp_path / "outside-target.txt"
    victim.write_text("must not change\n")
    data = root / "stopwords.zip"
    data.write_bytes(b"real corpus bytes")
    os.link(data, tmp_path / "twin.zip")
    (root / "stopwords.zip.dehl").symlink_to(victim)

    rewritten, unfixable = downloader.break_nltk_hardlinks(str(tmp_path / "nltk_data"))

    assert (rewritten, unfixable) == (1, [])
    assert victim.read_text() == "must not change\n"
    assert not data.is_symlink() and data.stat().st_nlink == 1
    assert data.read_bytes() == b"real corpus bytes"
    assert (root / "stopwords.zip.dehl").is_symlink(), "a planted file is left as it was"


def test_a_symlink_in_the_cache_is_left_alone(downloader, tmp_path):
    root = tmp_path / "nltk_data"
    root.mkdir()
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"x")
    os.link(outside, tmp_path / "outside-twin.zip")
    (root / "link.zip").symlink_to(outside)

    assert downloader.break_nltk_hardlinks(str(root)) == (0, [])
    assert (root / "link.zip").is_symlink()
    assert outside.stat().st_nlink == 2


def test_concurrent_runs_leave_every_file_intact_and_unlinked(downloader, tmp_path):
    import threading

    root = tmp_path / "nltk_data"
    twin = tmp_path / "twin"
    for i in range(40):
        f = root / f"d{i % 4}" / f"f{i}.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(f"content {i}\n".encode() * 200)
        (twin / f.parent.name).mkdir(parents=True, exist_ok=True)
        os.link(f, twin / f.parent.name / f.name)

    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def _run():
        try:
            barrier.wait(timeout=10)
            downloader.break_nltk_hardlinks(str(root))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    files = sorted(p for p in root.rglob("*") if p.is_file())
    assert errors == []
    assert len(files) == 40, f"temp files left behind: {[p.name for p in files][:5]}"
    for i in range(40):
        f = root / f"d{i % 4}" / f"f{i}.txt"
        assert f.read_bytes() == f"content {i}\n".encode() * 200
        assert f.stat().st_nlink == 1


def test_an_image_without_nltk_offline_still_downloads(
    downloader, hardlinked_cache, monkeypatch, capsys
):
    """Older images lack app.utils.nltk_offline; that must degrade with a message, not fail."""
    import sys

    monkeypatch.setitem(sys.modules, "app.utils.nltk_offline", None)

    result = downloader.download_nltk_data()["nltk"]

    assert result["status"] == "downloaded", result
    assert "predates app.utils.nltk_offline" in capsys.readouterr().out
