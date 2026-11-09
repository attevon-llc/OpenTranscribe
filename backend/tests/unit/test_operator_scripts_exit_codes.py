"""Operator and benchmark scripts must exit nonzero when any item failed."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"operator_script_{name}", _SCRIPTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _error_file() -> MagicMock:
    file = MagicMock()
    file.id = 7
    file.uuid = "00000000-0000-0000-0000-000000000007"
    file.filename = "a.mp4"
    file.source_url = "https://youtube.com/watch?v=x"
    file.user_id = 1
    return file


def test_fix_error_status_exits_nonzero_when_reset_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load("fix_error_status")
    category = MagicMock(value="network")
    monkeypatch.setattr(sys, "argv", ["fix_error_status.py"])
    monkeypatch.setattr(mod, "create_engine", MagicMock())
    monkeypatch.setattr(mod, "sessionmaker", MagicMock())
    monkeypatch.setattr(
        mod, "_categorize_files", lambda db, files: ([], [(_error_file(), category)], [])
    )
    monkeypatch.setattr(mod, "reset_file_for_retry", lambda *a, **k: False)
    assert mod.main() == 1


def test_fix_error_status_exits_zero_when_nothing_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load("fix_error_status")
    monkeypatch.setattr(sys, "argv", ["fix_error_status.py"])
    monkeypatch.setattr(mod, "create_engine", MagicMock())
    monkeypatch.setattr(mod, "sessionmaker", MagicMock())
    monkeypatch.setattr(mod, "_categorize_files", lambda db, files: ([], [], []))
    assert mod.main() == 0


def test_retry_youtube_exits_nonzero_when_reset_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load("retry_youtube_auth_errors_staggered")
    session = MagicMock()
    chain = session.query.return_value.filter.return_value.order_by.return_value
    chain.all.return_value = [_error_file()]
    monkeypatch.setattr(sys, "argv", ["retry.py", "--batch-size", "1", "--delay", "0"])
    monkeypatch.setattr(mod, "create_engine", MagicMock())
    monkeypatch.setattr(mod, "sessionmaker", MagicMock(return_value=lambda: session))
    monkeypatch.setattr(mod, "reset_file_for_retry", lambda *a, **k: False)
    assert mod.main() == 1


def _migration_patches(files: list[MagicMock], prepare: MagicMock):
    session = MagicMock()
    session.query.return_value.filter.return_value.limit.return_value.all.return_value = files
    return (
        patch("sqlalchemy.create_engine", MagicMock()),
        patch("sqlalchemy.orm.sessionmaker", MagicMock(return_value=lambda: session)),
        patch("app.tasks.migration_pipeline.prepare_file", prepare),
        patch("app.services.speaker_embedding_service.get_cached_embedding_service", MagicMock()),
        patch("app.services.speaker_analysis_models.MultiModelRunner", MagicMock()),
        patch("app.services.speaker_analysis_models.EmbeddingModelAdapter", MagicMock()),
    )


def _run_migration_benchmark(
    monkeypatch: pytest.MonkeyPatch, files: list[MagicMock], prepare: MagicMock
) -> int | None:
    from contextlib import ExitStack

    mod = _load("benchmark_migration")
    monkeypatch.setattr(sys, "argv", ["benchmark_migration.py"])
    with ExitStack() as stack:
        for p in _migration_patches(files, prepare):
            stack.enter_context(p)
        code: int | None = mod.main()
    return code


def test_benchmark_migration_exits_nonzero_when_no_files(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_migration_benchmark(monkeypatch, [], MagicMock()) == 1


def test_benchmark_migration_exits_nonzero_when_every_file_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = _error_file()
    file.file_size = 1024 * 1024
    prepare = MagicMock(side_effect=RuntimeError("boom"))
    assert _run_migration_benchmark(monkeypatch, [file], prepare) == 1


def test_measure_dataset_boundaries_surfaces_missing_metrics_package() -> None:
    mod = _load("measure_dataset_boundaries")
    with patch.object(mod, "der", side_effect=ImportError("pyannote.metrics")):
        with pytest.raises(ImportError):
            mod._der_or_none([], [])


def test_measure_dataset_boundaries_records_none_for_undefined_der() -> None:
    mod = _load("measure_dataset_boundaries")
    with patch.object(mod, "der", side_effect=ValueError("no reference")):
        assert mod._der_or_none([], []) is None
