"""Sweep registration for corpus-injected test files and ``benchmark_parallel.py`` uploads (#1222).

Both used to be unsweepable: an injected meeting is named ``<corpus>__<meeting>.transcript``
(the corpus's name, not ours) and a standalone ``benchmark_parallel.py --upload`` sent the
corpus's REAL filenames, so neither could be told from a user's own data. Now the injector
stamps a seed that tests mint in a registered shape, and the benchmark uploads under
``bench-parallel-<hex8>``.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[3]
_TESTS = _REPO / "backend" / "tests"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cleanup = _load("cleanup_test_data_injected", _REPO / "scripts" / "cleanup-test-data.py")


@pytest.fixture(scope="module")
def bench(tmp_path_factory) -> ModuleType:
    # The script reconfigures sys.stdout at import; pytest's capture stream may refuse that.
    return _load("benchmark_parallel_under_test", _REPO / "scripts" / "benchmark_parallel.py")


def _bench_spec() -> Any:
    return next(s for s in cleanup.MEDIA_FILENAME_SPECS if s.label == "bench-parallel")


class TestBenchParallelUploads:
    def test_the_uploaded_name_is_a_registered_shape_and_hides_the_corpus_name(self, bench):
        name = bench._upload_name("seed_t3_900s_ab12cd34_Real Interview.MP3")
        assert _bench_spec().shape.match(name), name
        assert "Interview" not in name
        assert name.endswith(".mp3")

    def test_two_uploads_of_one_corpus_file_get_different_names(self, bench):
        assert bench._upload_name("a.wav") != bench._upload_name("a.wav")

    @pytest.mark.parametrize(
        ("filename", "swept"),
        [
            ("bench-parallel-0123abcd.wav", True),
            ("bench-parallel-0123abcd.mp3", True),
            ("bench-parallel-notes.wav", False),  # a bare prefix is a human's file
            ("bench-parallel-0123abcd", False),
            ("seed_t3_900s_ab12cd34.wav", False),  # the old behaviour: a real corpus name
        ],
    )
    def test_shape_sweeps_only_the_uniquified_name(self, filename, swept):
        assert bool(_bench_spec().shape.match(filename)) is swept

    def test_upload_posts_the_registered_name_not_the_corpus_name(
        self, bench, tmp_path, monkeypatch
    ):
        audio = tmp_path / "Real Interview.wav"
        audio.write_bytes(b"RIFFxxxxWAVE")
        seen: dict[str, Any] = {}

        class _Resp:
            status_code = 200
            text = ""

            def json(self) -> dict[str, str]:
                return {"uuid": "11111111-1111-1111-1111-111111111111"}

        def fake_post(url, headers=None, files=None, timeout=None):
            seen["filename"] = files["file"][0]
            return _Resp()

        monkeypatch.setattr(bench.requests, "post", fake_post)
        monkeypatch.setattr(bench, "_UPLOADED_UUIDS", [])
        assert bench.upload_file("tok", str(audio)) == "11111111-1111-1111-1111-111111111111"
        assert _bench_spec().shape.match(seen["filename"]), seen["filename"]
        assert bench._UPLOADED_UUIDS == ["11111111-1111-1111-1111-111111111111"]

    def test_cleanup_falls_back_to_force_and_counts_what_it_could_not_delete(
        self, bench, monkeypatch
    ):
        calls: list[str] = []

        class _R:
            def __init__(self, code: int) -> None:
                self.status_code = code

        def fake_delete(url, headers=None, timeout=None):
            calls.append(url.rsplit("/api/files/", 1)[1])
            if url.endswith("/gone"):
                return _R(404)
            if url.endswith(("/stuck", "/stuck/force")):
                return _R(500)
            return _R(409) if not url.endswith("/force") else _R(200)

        monkeypatch.setattr(bench.requests, "delete", fake_delete)
        assert bench.delete_uploaded_files("tok", ["busy", "gone", "stuck"]) == 1
        assert calls == ["busy", "busy/force", "gone", "gone/force", "stuck", "stuck/force"]


class TestInjectedTestSeeds:
    @pytest.mark.parametrize(
        ("seed", "swept"),
        [
            ("pytest-0123456789ab", True),
            ("pytest-synth-0123456789", True),
            ("pytest-searchqual-0123456789", True),
            ("e2e-owned-0123456789", True),
            ("", False),  # the canonical eval corpus
            ("rag403", False),  # an operator's namespace
            ("pytest-notes", False),
            ("pytest-synth-0123456789-extra", False),
        ],
    )
    def test_only_uniquified_test_seeds_are_candidates(self, seed, swept):
        assert bool(cleanup.INJECTED_TEST_SEED_SHAPE.fullmatch(seed)) is swept

    def test_every_seed_the_test_suites_mint_is_registered(self):
        """Scan ``seed = f"...{uuid.uuid4().hex[:N]}"`` under backend/tests and render it."""
        minted = re.compile(
            r'^\s*(?:SEED|seed)\s*=\s*f"([^"{}]*)\{uuid\.uuid4\(\)\.hex\[:(\d+)\]\}"\s*$',
            re.MULTILINE,
        )
        rendered: dict[str, str] = {}
        for path in _TESTS.rglob("*.py"):
            if path.name == Path(__file__).name:
                continue
            for prefix, width in minted.findall(path.read_text()):
                rendered[f"{path.relative_to(_TESTS)}:{prefix}"] = prefix + "a" * int(width)
        assert len(rendered) >= 4, f"seed scan found too little to trust: {rendered}"
        unregistered = {
            key: seed
            for key, seed in rendered.items()
            if not cleanup.INJECTED_TEST_SEED_SHAPE.fullmatch(seed)
        }
        assert not unregistered, (
            "a test mints an injector seed the sweep would never recognise "
            f"(register it in INJECTED_TEST_SEED_SHAPE): {unregistered}"
        )
