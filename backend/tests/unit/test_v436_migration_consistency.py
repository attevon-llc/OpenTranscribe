"""v436 (lite: "this server" speaker detection -> Automatic) static consistency (issue #1228).

What can be checked without a database lives here: the chain, vendor neutrality, that the
frozen (provider, model) -> returns-speakers table agrees with the SQL that applies it AND
with today's catalog, that the revision is a no-op on the full image, and that the
downgrade is one. The data behaviour (rewrite, idempotence, up/down, detection) is proven
against a throwaway Postgres in ``tests/integration/test_v436_lite_settings_migration.py``,
never against a live stack's database.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import re
import textwrap
from pathlib import Path

import pytest

REVISION = "v436_lite_local_speaker_detection_to_automatic"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v436_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v434_reset_unusable_pyannote_source"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_the_revision_is_vendor_neutral():
    source = _PATH.read_text().lower()
    assert "clerk" not in source
    assert "stripe" not in source


def _sql_list(sql: str, column_clause: str) -> set[str]:
    match = re.search(rf"{re.escape(column_clause)} IN \(([^)]*)\)", sql)
    assert match, f"no {column_clause} IN (...) in the predicate"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_the_frozen_table_is_what_the_sql_applies():
    module = _module()
    sql = module.LITE_LOCAL_WITH_SPEAKERS_WHERE

    assert _sql_list(sql, "c.provider") == set(module.PROVIDERS_WITH_SPEAKERS)
    assert set(module.MODELS_WITH_SPEAKERS) == {"openai"}
    assert _sql_list(sql, "c.model_name") == set(module.MODELS_WITH_SPEAKERS["openai"])
    assert "c.provider = 'openai'" in sql


def test_the_frozen_table_matched_the_catalog_when_written():
    """The revision must not import the catalog; this pins that it was RIGHT when frozen.

    If a later catalog change makes this fail, the revision stays as it is (it describes the
    rule of its own time): update this test's expectation, never the revision.
    """
    from app.services.asr.factory import ASR_PROVIDER_CATALOG
    from app.services.asr.factory import ASRProviderFactory

    module = _module()
    returns_speakers = {
        (provider, model["id"])
        for provider, entry in ASR_PROVIDER_CATALOG.items()
        if provider != "local"
        for model in entry.get("models", [])
        if ASRProviderFactory.get_model_capabilities(provider, model["id"])["supports_diarization"]
    }
    frozen = {
        (provider, model["id"])
        for provider in module.PROVIDERS_WITH_SPEAKERS
        for model in ASR_PROVIDER_CATALOG[provider].get("models", [])
    } | {(p, m) for p, models in module.MODELS_WITH_SPEAKERS.items() for m in models}

    assert returns_speakers, "the catalog sweep found no speaker model: it proves nothing"
    assert frozen == returns_speakers


@pytest.mark.parametrize(("mode", "runs"), [("lite", True), ("LITE ", True), ("full", False)])
def test_the_rewrite_runs_on_lite_only(monkeypatch, mode, runs):
    module = _module()
    monkeypatch.setenv("DEPLOYMENT_MODE", mode)

    assert module.statements() == ([module.RESET_SQL] if runs else [])


def test_upgrade_runs_exactly_the_planned_statements():
    """``upgrade`` is a loop over :func:`statements`, so the lite gate above is its gate."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(_module().upgrade)))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    assert len(func.body) == 1
    loop = func.body[0]
    assert isinstance(loop, ast.For)
    assert isinstance(loop.iter, ast.Call) and getattr(loop.iter.func, "id", None) == "statements"


def test_an_unset_deployment_mode_is_the_full_image(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    assert _module().deployment_is_lite() is False


def test_the_downgrade_is_a_no_op():
    """Nothing records which rows were rewritten, so there is nothing to restore."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(_module().downgrade)))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    assert len(func.body) == 1
    assert isinstance(func.body[0], ast.Expr)
    assert isinstance(func.body[0].value, ast.Constant)
