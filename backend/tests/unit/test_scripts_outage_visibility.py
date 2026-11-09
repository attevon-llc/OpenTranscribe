"""Outage-injection tests for the B11 broad-except lane over ``scripts/``.

Each test injects the failure a script's handler used to swallow (or mis-report) and asserts
that the failure is visible: in the exit status, the returned/recorded value, and a logged
traceback (``caplog`` record with ``exc_info``). A handler that again collapses "could not
look" into "found nothing" fails here.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest
import requests
from sqlalchemy.exc import OperationalError

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "scripts"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _logged_traceback(caplog: pytest.LogCaptureFixture, fragment: str) -> bool:
    return any(
        fragment in record.getMessage() and record.exc_info is not None for record in caplog.records
    )


@pytest.fixture
def scripts_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(_SCRIPTS))


def test_nltk_download_that_returns_false_is_a_failed_group(
    scripts_on_path: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``nltk.download`` reports a failed fetch by returning False; it used to count as ok."""
    import nltk
    from model_download.nltk import download_nltk_data

    monkeypatch.setenv("NLTK_DATA", str(tmp_path))
    monkeypatch.setattr(nltk, "download", lambda package, **_kw: package != "stopwords")

    result = download_nltk_data()

    assert result["nltk"]["status"] == "failed"
    assert "stopwords" in result["nltk"]["error"]


def test_orphan_profile_report_records_an_unreachable_index(
    scripts_on_path: None, caplog: pytest.LogCaptureFixture
) -> None:
    from cleanup_test_data.planes import _report_orphan_profile_docs

    plane = MagicMock()
    plane.session.post.side_effect = requests.ConnectionError("opensearch down")
    report: dict = {"resources": {}}

    with caplog.at_level(logging.WARNING):
        _report_orphan_profile_docs(plane, report)

    assert "opensearch down" in report["resources"]["orphan_profile_docs"]["error"]
    assert _logged_traceback(caplog, "speaker profile docs")


def test_orphan_profile_report_does_not_swallow_a_programming_error(
    scripts_on_path: None,
) -> None:
    from cleanup_test_data.planes import _report_orphan_profile_docs

    plane = MagicMock()
    plane.session.post.side_effect = TypeError("bug, not an outage")

    with pytest.raises(TypeError):
        _report_orphan_profile_docs(plane, {"resources": {}})


def test_failed_artifact_delete_is_counted_and_logged(
    scripts_on_path: None, caplog: pytest.LogCaptureFixture
) -> None:
    from cleanup_test_data.planes import _delete_artifacts

    finding = MagicMock()
    finding.name = "ot-test-index"
    plane = MagicMock()
    plane.delete_one.side_effect = requests.ConnectionError("store down")

    with caplog.at_level(logging.WARNING):
        deleted, any_failed = _delete_artifacts(plane, [finding], 0, include_tier_b=False)

    assert (deleted, any_failed) == (0, True)
    assert _logged_traceback(caplog, "ot-test-index")


def test_soak_seed_reports_failed_downloads(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    soak = _load(_SCRIPTS / "soak_seed_audio.py", "soak_seed_audio_under_test")

    def boom(*_args: object, **_kwargs: object) -> Path:
        raise requests.ConnectionError("nas down")

    monkeypatch.setattr(soak, "download_one", boom)
    picks = [(1, {"uuid": "abcdef12-0000", "filename": "a.wav"})]

    with caplog.at_level(logging.WARNING):
        failures = soak.download_many("http://x", "tok", picks, tmp_path, workers=2)

    assert failures == 1
    assert _logged_traceback(caplog, "abcdef12-0000")


def test_task_id_lookup_logs_a_backend_outage(
    scripts_on_path: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from e2e_bench import backend_api

    def boom(*_a: object, **_k: object) -> None:
        raise requests.ConnectionError("backend down")

    monkeypatch.setattr(backend_api.requests, "get", boom)

    with caplog.at_level(logging.WARNING):
        assert backend_api.get_task_id_for_file_via_api("http://x", "uuid-1", "tok") == ""

    assert _logged_traceback(caplog, "uuid-1")


def test_user_cleanup_counts_a_failed_delete_and_logs_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cleanup_users = _load(_SCRIPTS / "cleanup-test-users.py", "cleanup_test_users_under_test")
    conn = MagicMock()
    conn.begin_nested.side_effect = OperationalError("DELETE", {}, Exception("db down"))

    with caplog.at_level(logging.WARNING):
        removed, failed = cleanup_users._delete_users(
            conn, [(1, "probe@example.com")], execute=True
        )

    assert (removed, failed) == ([], ["probe@example.com"])
    assert _logged_traceback(caplog, "probe@example.com")


def test_schema_drift_exits_3_with_a_traceback_when_the_db_is_down(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    drift = _load(_SCRIPTS / "check-schema-drift.py", "check_schema_drift_under_test")

    def boom() -> list:
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    monkeypatch.setattr(drift, "collect_diffs", boom)
    monkeypatch.setattr(drift, "_bootstrap_env", lambda: None)
    monkeypatch.setattr(drift, "BACKEND", tmp_path)
    monkeypatch.setattr(sys, "argv", ["check-schema-drift.py"])
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING):
        assert drift.main() == 3

    assert _logged_traceback(caplog, "schema comparison failed")


def test_vram_sampler_logs_a_missing_nvidia_smi(
    scripts_on_path: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from bench_parallel import monitors

    def boom(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(monitors.subprocess, "run", boom)
    sampler = monitors.VramMonitor.__new__(monitors.VramMonitor)
    sampler.gpu_id = 0

    with caplog.at_level(logging.WARNING):
        assert sampler._sample() is None

    assert any(r.exc_info is not None for r in caplog.records)
