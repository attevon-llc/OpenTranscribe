"""`download-models.py --only opensearch` must not require a Hugging Face token.

The OpenSearch neural models come from artifacts.opensearch.org, not Hugging Face, and
`ensure_opensearch_models` (scripts/common.sh) runs exactly that selector. With the group
missing from GROUPS_WITHOUT_HF_TOKEN the downloader exited 1 on any install without a
token, before fetching anything.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

DOWNLOADER_PY = Path(__file__).resolve().parents[3] / "scripts" / "download-models.py"

pytestmark = pytest.mark.skipif(
    not DOWNLOADER_PY.exists(), reason="scripts/download-models.py not present in this checkout"
)


def _load():
    spec = importlib.util.spec_from_file_location("download_models_under_test", DOWNLOADER_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("group", "needs_token"), [("opensearch", False), ("pyannote", True)])
def test_token_requirement_per_group(monkeypatch, tmp_path, group, needs_token):
    mod = _load()
    calls: list[str] = []

    def fake_download() -> dict[str, object]:
        calls.append(group)
        return {}

    monkeypatch.setitem(mod.DOWNLOAD_GROUPS, group, fake_download)
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["download-models.py", "--only", group])
    exit_code = 0
    try:
        mod.main()
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    if needs_token:
        assert calls == [] and exit_code == 1, "a Hugging Face group ran without a token"
    else:
        assert calls == [group], (
            f"--only {group} was refused for a missing HF token (exit {exit_code})"
        )
