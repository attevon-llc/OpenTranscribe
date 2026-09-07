"""``SEARCH_SEMANTIC_HIGH_CONFIDENCE`` has exactly one value, read from ``settings``.

As of issue #698, only ONE call site in ``hybrid_search_service`` may decide
``SearchHit.semantic_confidence`` from this threshold: ``_process_collapsed_results``, and only
on the fused hybrid-RRF path (``is_fused_rrf=True``). ``SEARCH_SEMANTIC_HIGH_CONFIDENCE`` was
tuned against RRF scores (~0.016 at rank 1); comparing it against a raw ``cosinesimil`` score
(``_bm25_leg_is_starved`` arm, [0, 1], 0.5 = orthogonal) or a raw BM25 score
(``_build_search_hit_from_bucket``, Phase 2, ~1-30) reads "high" for nearly every result — see
issue #698. ``_build_search_hit_from_bucket`` therefore no longer reads the setting at all; it
never labels confidence (``semantic_confidence`` stays ``""``), which is honest for a BM25-space
score.

This file previously pinned the earlier, source-level "the setting has one dead-code-free value,
read identically at both sites" invariant from PR #697/#698's prerequisite. That invariant is
superseded: there is now deliberately only one reader, and the two paths deliberately no longer
agree (one labels, one never does). ``test_no_call_site_supplies_an_inline_default`` remains —
still true, still worth guarding — and per-score-space behaviour is covered in
``test_semantic_confidence_score_spaces_698.py``.

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

# The one function permitted to read the setting (#698: `_build_search_hit_from_bucket`'s
# Phase 2 scores are raw BM25, not RRF, so it must never read this threshold at all).
EXPECTED_READER_FUNCTIONS = {
    "_process_collapsed_results",
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


def _confidence_from_collapsed_path(score: float, *, is_fused_rrf: bool = True) -> str:
    hits, _ = HybridSearchService()._process_collapsed_results(
        _collapsed_response(score), "", is_fused_rrf=is_fused_rrf
    )
    assert len(hits) == 1, "fixture should yield exactly one collapsed file group"
    return hits[0].semantic_confidence


def _confidence_from_two_phase_path(score: float) -> str:
    bucket, phase2 = _bucket_and_phase2(score)
    hit = HybridSearchService()._build_search_hit_from_bucket(bucket, phase2, "")
    assert hit is not None, "fixture should yield a SearchHit"
    return hit.semantic_confidence


@pytest.mark.parametrize("threshold", [0.010, 0.25])
def test_the_fused_path_switches_at_the_configured_threshold(monkeypatch, threshold):
    """The one remaining classifier moves when the single setting moves.

    Probed just below, exactly at, and just above the configured value, on the fused-RRF path
    only (`is_fused_rrf=True`) — the only case this threshold is ever compared against.
    """
    monkeypatch.setattr(settings, SETTING_NAME, threshold)
    below, at, above = threshold - 0.001, threshold, threshold + 0.001

    assert _confidence_from_collapsed_path(below) == "low"
    assert _confidence_from_collapsed_path(at) == "high"
    assert _confidence_from_collapsed_path(above) == "high"


@pytest.mark.parametrize("score", [0.005, 0.0125, 0.02, 999.0])
def test_the_two_phase_path_never_labels_confidence(score):
    """`_build_search_hit_from_bucket` (Phase 2, raw BM25 scores) never reads the threshold.

    Any score — including one far outside RRF's ~0-0.065 range — must produce no badge rather
    than a meaningless "high". This is the issue #698 regression: before the fix this path read
    "high" for nearly every semantic-only Phase-2 result.
    """
    assert _confidence_from_two_phase_path(score) == ""
