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
