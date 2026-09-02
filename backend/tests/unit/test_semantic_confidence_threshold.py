"""``SEARCH_SEMANTIC_HIGH_CONFIDENCE`` has exactly one value, read from ``settings``.

Two call sites in ``hybrid_search_service`` decide ``SearchHit.semantic_confidence``
(``"high"`` / ``"low"``) for a semantic-only file:

* ``_process_collapsed_results``   — the single-phase collapse path
* ``_build_search_hit_from_bucket`` — the two-phase (non-relevance sort) path

Both read the setting, and both used to read it through ``getattr(settings, ...)`` with a
**divergent** inline fallback: ``0.015`` at the first site, ``0.010`` at the second. Because
``SEARCH_SEMANTIC_HIGH_CONFIDENCE`` is a declared field on the ``BaseSettings`` subclass it can
never be absent, so *both* fallbacks were unreachable and the effective threshold was ``0.010``
at both sites. The code nonetheless read as if two different thresholds were in play, and the
next reader to "honour" the 0.015 would have changed live ranking behaviour by accident.

The guard below is source-level on purpose: a behavioural test cannot see a dead default, so
it cannot fail when one is reintroduced. ``test_no_call_site_supplies_an_inline_default`` is the
red-before/green-after assertion; the behavioural pair beside it pins that both sites actually
track ``settings`` rather than a literal of their own.

Pattern follows ``tests/api/test_file_mutation_permissions.py``'s ``MUTATING_ENDPOINTS``
anti-drift guard — AST, not grep, so a mention in a docstring or comment cannot satisfy it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.core.config import Settings
from app.core.config import settings
from app.services.search.hybrid_search_service import HybridSearchService

SETTING_NAME = "SEARCH_SEMANTIC_HIGH_CONFIDENCE"

APP_DIR = pathlib.Path(__file__).resolve().parents[2] / "app"
SERVICE_PATH = APP_DIR / "services" / "search" / "hybrid_search_service.py"

# The two functions that classify a semantic-only hit's confidence. Both must read the
# setting, and neither may carry a default of its own.
EXPECTED_READER_FUNCTIONS = {
    "_process_collapsed_results",
    "_build_search_hit_from_bucket",
}


def _enclosing_function_by_lineno(tree: ast.AST) -> dict[int, str]:
    """Map every line inside a function body to that function's name."""
    spans: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            for line in range(node.lineno, end + 1):
                spans[line] = node.name
    return spans


def _attribute_reads(path: pathlib.Path) -> list[tuple[str, int]]:
    """``(enclosing function, lineno)`` for each ``<something>.SETTING_NAME`` read."""
    tree = ast.parse(path.read_text(), filename=str(path))
    spans = _enclosing_function_by_lineno(tree)
    return sorted(
        (spans.get(node.lineno, "<module>"), node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == SETTING_NAME
    )


def _getattr_reads_with_default(path: pathlib.Path) -> list[tuple[str, int, object]]:
    """``(enclosing function, lineno, default)`` for ``getattr(x, "SETTING_NAME", <default>)``."""
    tree = ast.parse(path.read_text(), filename=str(path))
    spans = _enclosing_function_by_lineno(tree)
    found: list[tuple[str, int, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "getattr"):
            continue
        if len(node.args) < 2:
            continue
        name_arg = node.args[1]
        if not (isinstance(name_arg, ast.Constant) and name_arg.value == SETTING_NAME):
            continue
        default: object = None
        if len(node.args) > 2:
            default_node = node.args[2]
            # A non-literal default is still an inline default; report it by source shape.
            default = (
                default_node.value
                if isinstance(default_node, ast.Constant)
                else ast.unparse(default_node)
            )
        found.append((spans.get(node.lineno, "<module>"), node.lineno, default))
    return sorted(found, key=lambda item: item[1])


def test_the_setting_is_declared_so_no_fallback_can_ever_be_reached():
    """Evidence that the removed inline defaults were dead code, not live behaviour.

    ``Settings`` is a ``BaseSettings`` subclass and ``SEARCH_SEMANTIC_HIGH_CONFIDENCE`` is a
    declared field with a default, so every instance carries the attribute. A
    ``getattr(settings, ..., <fallback>)`` therefore never returns its fallback.
    """
    assert SETTING_NAME in Settings.model_fields
    assert hasattr(settings, SETTING_NAME)
    assert Settings.model_fields[SETTING_NAME].default == pytest.approx(0.010)


def test_no_call_site_supplies_an_inline_default():
    """RED before the fix: both sites passed a (different) literal fallback to ``getattr``.

    Fails with the exact function and line of any ``getattr(..., "SEARCH_SEMANTIC_HIGH_
    CONFIDENCE", <default>)`` anywhere under ``app/`` — the shape that lets a second,
    unreachable value for this threshold re-enter the codebase.
    """
    offenders: list[tuple[str, str, int, object]] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for func, lineno, default in _getattr_reads_with_default(path):
            offenders.append((str(path.relative_to(APP_DIR)), func, lineno, default))

    assert not offenders, (
        f"{SETTING_NAME} must be read as settings.{SETTING_NAME} with no inline default. "
        f"Offending getattr call site(s) (file, function, line, default): {offenders}. "
        "The value lives in app/core/config.py and nowhere else."
    )


def test_both_confidence_call_sites_read_the_setting_directly():
    """Anti-drift: exactly the two classification functions read the setting, via attribute.

    Catches a site being deleted (losing the classification) as well as a third one appearing
    without this guard being updated.
    """
    reads = _attribute_reads(SERVICE_PATH)
    functions = {func for func, _ in reads}

    assert functions == EXPECTED_READER_FUNCTIONS, (
        f"{SETTING_NAME} attribute reads in {SERVICE_PATH.name} live in {sorted(functions)}, "
        f"expected {sorted(EXPECTED_READER_FUNCTIONS)}. Reads found at: {reads}"
    )
    assert len(reads) == len(EXPECTED_READER_FUNCTIONS), (
        f"Expected exactly one read per classification function, found {reads}"
    )


def _collapsed_response(score: float) -> dict:
    """A one-file collapsed response whose single inner hit is semantic-only.

    No ``highlight`` on the inner hit and an empty query mean ``_process_inner_hits`` records
    ``keyword_count == 0``, which is the ``is_semantic_only`` branch under test.
    """
    return {
        "hits": {
            "hits": [
                {
                    "_score": score,
                    "_source": {
                        "file_uuid": "file-under-test",
                        "file_id": 1,
                        "title": "Quarterly planning",
                        "speakers": [],
                        "tags": [],
                        "language": "en",
                        "content_type": "audio/wav",
                    },
                    "inner_hits": {
                        "segments": {
                            "hits": {
                                "total": {"value": 1},
                                "hits": [
                                    {
                                        "_score": score,
                                        "_source": {
                                            "content": "we should revisit the roadmap",
                                            "speaker": "SPEAKER_00",
                                            "start_time": 0.0,
                                            "end_time": 3.0,
                                            "chunk_index": 0,
                                        },
                                    }
                                ],
                            }
                        }
                    },
                }
            ]
        }
    }


def _bucket_and_phase2(score: float) -> tuple[dict, dict]:
    """A Phase-1 bucket plus the Phase-2 lookup entry for the same file, semantic-only."""
    bucket = {
        "key": "file-under-test",
        "title_kw": {"buckets": [{"key": "Quarterly planning"}]},
        "language_kw": {"buckets": [{"key": "en"}]},
        "content_type_kw": {"buckets": [{"key": "audio/wav"}]},
        "speakers_kw": {"buckets": []},
        "tags_kw": {"buckets": []},
        "max_duration": {"value": 60.0},
        "max_file_size": {"value": 1024},
        "max_upload_time": {"value_as_string": "2026-01-01T00:00:00Z"},
        "min_file_id": {"value": 1},
    }
    phase2 = {
        "occurrences": [object()],
        "title_highlighted": "",
        "match_sources": [],
        "keyword_count": 0,
        "semantic_count": 1,
        "best_score": score,
    }
    return bucket, phase2


def _confidence_from_collapsed_path(score: float) -> str:
    hits, _ = HybridSearchService()._process_collapsed_results(_collapsed_response(score), "")
    assert len(hits) == 1, "fixture should yield exactly one collapsed file group"
    return hits[0].semantic_confidence


def _confidence_from_two_phase_path(score: float) -> str:
    bucket, phase2 = _bucket_and_phase2(score)
    hit = HybridSearchService()._build_search_hit_from_bucket(bucket, phase2, "")
    assert hit is not None, "fixture should yield a SearchHit"
    return hit.semantic_confidence


@pytest.mark.parametrize("threshold", [0.010, 0.25])
def test_both_paths_switch_at_the_same_configured_threshold(monkeypatch, threshold):
    """Both classification paths must move together when the single setting moves.

    Probed just below, exactly at, and just above the configured value. A site carrying its own
    literal instead of the setting disagrees with the other at the ``0.25`` parametrization.
    """
    monkeypatch.setattr(settings, SETTING_NAME, threshold)
    below, at, above = threshold - 0.001, threshold, threshold + 0.001

    assert _confidence_from_collapsed_path(below) == "low"
    assert _confidence_from_two_phase_path(below) == "low"
    assert _confidence_from_collapsed_path(at) == "high"
    assert _confidence_from_two_phase_path(at) == "high"
    assert _confidence_from_collapsed_path(above) == "high"
    assert _confidence_from_two_phase_path(above) == "high"


@pytest.mark.parametrize("score", [0.005, 0.0125, 0.02])
def test_the_two_paths_agree_on_every_probe_score(score):
    """The two sites classify identically at the deployed threshold.

    ``0.0125`` sits between the two former inline defaults (0.010 and 0.015), so this probe is
    the one that would disagree if a site ever honoured a 0.015 of its own.
    """
    assert _confidence_from_collapsed_path(score) == _confidence_from_two_phase_path(score)
