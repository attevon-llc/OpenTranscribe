"""The vendored legal documents must match MANIFEST.json byte for byte."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from app.legal import registry

DOCS = Path(registry.__file__).resolve().parent / "documents"
SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "verify_vendored.py"


def _verifier():
    spec = importlib.util.spec_from_file_location("verify_vendored", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _problems(folder: Path) -> list[str]:
    manifest = json.loads((folder / "MANIFEST.json").read_text())
    v = _verifier()
    out = []
    for name, meta in manifest["files"].items():
        f = folder / name
        if not f.is_file():
            out.append(f"{name}: missing")
        elif v.sha256(f) != meta["sha256"]:
            out.append(f"{name}: hash differs")
    return out


def test_manifest_lists_exactly_the_four_documents():
    manifest = json.loads((DOCS / "MANIFEST.json").read_text())
    assert sorted(manifest["files"]) == [
        "data-flows-annex.md",
        "operator-acknowledgment.md",
        "privacy-notice.md",
        "terms-of-use.md",
    ]
    assert manifest["tag"] == "e7bb629"


def test_vendored_files_match_manifest():
    assert _problems(DOCS) == []


def test_registry_keys_and_versions_match_manifest():
    manifest = json.loads((DOCS / "MANIFEST.json").read_text())
    served = {d.key: d.version for d in registry.list_documents()}
    assert served == {m["key"]: m["version"] for m in manifest["files"].values()}
    assert all(d.status == "draft" for d in registry.list_documents())


def test_tampered_copy_is_detected(tmp_path):
    shutil.copytree(DOCS, tmp_path / "d")
    assert _problems(tmp_path / "d") == []  # control: the untouched copy is clean
    target = tmp_path / "d" / "terms-of-use.md"
    target.write_bytes(target.read_bytes() + b"\nextra line\n")
    assert _problems(tmp_path / "d") == ["terms-of-use.md: hash differs"]


@pytest.mark.parametrize("flag", ["--selftest"])
def test_verify_script_selftest(flag):
    assert _verifier().main([flag]) == 0
