"""download-models.py runs diar-native concurrently with the other groups (EFFC SU1).

diar-native exports from the pyannote weights, so pyannote must finish first; after that the
diar-native child process (the same ``--only diar-native`` path release scenario D proves)
overlaps every remaining group. One manifest is written by the parent, and any group failing
still exits 1.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

DOWNLOADER_PY = Path(__file__).resolve().parents[3] / "scripts" / "download-models.py"


@pytest.fixture(autouse=True)
def _restore_torch_patches():
    torch = pytest.importorskip("torch")
    saved_load = torch.load
    saved_empty_cache = torch.mps.empty_cache
    try:
        yield
    finally:
        torch.load = saved_load
        torch.mps.empty_cache = saved_empty_cache


def _load():
    spec = importlib.util.spec_from_file_location("download_models_parallel", DOWNLOADER_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeProc:
    def __init__(self, events, results_path, child_results, rc):
        self.stdout = iter(["hello from child\n"])
        self._events, self._rc = events, rc
        Path(results_path).write_text(json.dumps(child_results))

    def wait(self):
        self._events.append("child-wait")
        return self._rc


def _install(mod, monkeypatch, events, *, child_results, rc=0):
    def group(name):
        def run():
            events.append(f"group:{name}")
            return {name: {"status": "downloaded"}}

        return run

    for g in ("pyannote", "nltk", "whisper"):
        monkeypatch.setitem(mod.DOWNLOAD_GROUPS, g, group(g))
    monkeypatch.setitem(mod.DOWNLOAD_GROUPS, "diar-native", group("diar-native-INPROCESS"))

    def popen(argv, **kwargs):
        events.append("child-start")
        assert argv[argv.index("--only") + 1] == "diar-native"
        assert "--no-manifest" in argv
        return _FakeProc(events, argv[argv.index("--results-out") + 1], child_results, rc)

    monkeypatch.setattr(mod.subprocess, "Popen", popen)


def test_pyannote_first_then_child_overlaps_remaining_groups(monkeypatch, capsys):
    mod = _load()
    events: list[str] = []
    _install(mod, monkeypatch, events, child_results={"diar-native": {"status": "downloaded"}})
    results = mod._run_groups(["pyannote", "diar-native", "nltk", "whisper"])
    assert events == [
        "group:pyannote",
        "child-start",
        "group:nltk",
        "group:whisper",
        "child-wait",
    ]
    assert "group:diar-native-INPROCESS" not in events
    assert set(results) == {"pyannote", "nltk", "whisper", "diar-native"}
    assert "[diar-native] hello from child" in capsys.readouterr().out


def test_child_failure_is_reported_failed(monkeypatch):
    mod = _load()
    events: list[str] = []
    _install(mod, monkeypatch, events, child_results={}, rc=1)
    results = mod._run_groups(["pyannote", "diar-native", "nltk"])
    assert results["diar-native"]["status"] == "failed"


def test_only_nltk_stays_in_process(monkeypatch):
    mod = _load()
    events: list[str] = []
    _install(mod, monkeypatch, events, child_results={})
    assert mod._run_groups(["nltk"]) == {"nltk": {"status": "downloaded"}}
    assert events == ["group:nltk"]


def test_diar_native_alone_stays_in_process(monkeypatch):
    mod = _load()
    events: list[str] = []
    _install(mod, monkeypatch, events, child_results={})
    mod._run_groups(["diar-native"])
    assert events == ["group:diar-native-INPROCESS"]


def test_main_writes_one_manifest_and_exits_1_on_failure(monkeypatch):
    mod = _load()
    manifests: list[dict] = []

    def fake_manifest(results: dict) -> dict:
        manifests.append(dict(results))
        return {"cache": {"total_size_gb": 0}}

    monkeypatch.setattr(mod, "create_manifest", fake_manifest)
    monkeypatch.setattr(
        mod,
        "_run_groups",
        lambda sel: {"a": {"status": "downloaded"}, "diar-native": {"status": "failed"}},
    )
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "x")
    monkeypatch.setattr(sys, "argv", ["download-models.py"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert len(manifests) == 1
