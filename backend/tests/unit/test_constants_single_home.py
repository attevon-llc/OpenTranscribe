"""Pin the value and the single definition site of formerly duplicated constants.

Each of these constants once lived as an independent copy in several modules, which can
drift silently (a threshold edited in one copy only).  The scan is an AST walk over
``backend/app`` for module-level assignments, so comments and docstrings that merely
mention a name do not count.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"

# (name, scope relative to app/, expected defining file relative to app/ or None for "none")
SINGLE_HOME: list[tuple[str, str, str | None]] = [
    ("FUZZY_MATCH_THRESHOLD", ".", "core/constants.py"),
    ("SAMPLE_RATE", ".", "core/pipeline_constants.py"),
    ("MAX_WORD_DURATION", ".", "core/pipeline_constants.py"),
    ("_MAX_WORD_DURATION", ".", None),
    ("ASR_SEGMENT_MIN_GAP_SECONDS", ".", "core/pipeline_constants.py"),
    ("ASR_SEGMENT_MAX_DURATION_SECONDS", ".", "core/pipeline_constants.py"),
    ("_MIN_GAP", "services/asr", None),
    ("_MAX_DUR", "services/asr", None),
    ("FILES_PER_BATCH_TASK", ".", "core/pipeline_constants.py"),
    ("_BATCH_SIZE", "tasks", None),
    ("MASKABLE_CATEGORIES", ".", "services/redaction/config.py"),
    ("TERMINAL_STATUSES", "services/watch_sources", "services/watch_sources/processing.py"),
    ("_TERMINAL_STATUSES", "services/watch_sources", None),
    ("_TERMINAL", "tasks/watch_source_tasks.py", None),
    ("GENERIC_URL_PATTERN", ".", "services/media_download_service.py"),
    ("YOUTUBE_URL_PATTERN", ".", "services/media_download_service.py"),
    ("SUPPORTED_DOMAINS", ".", "models/custom_vocabulary.py"),
]


def _module_level_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _definers(name: str, scope: str) -> list[str]:
    root = APP / scope
    files = [root] if root.is_file() else sorted(root.rglob("*.py"))
    return [
        str(p.relative_to(APP))
        for p in files
        if "__pycache__" not in p.parts and name in _module_level_names(p)
    ]


@pytest.mark.parametrize(("name", "scope", "home"), SINGLE_HOME)
def test_constant_has_exactly_one_definition(name: str, scope: str, home: str | None) -> None:
    assert _definers(name, scope) == ([home] if home else [])


def test_pinned_values() -> None:
    from app.core import constants
    from app.core import pipeline_constants
    from app.models import custom_vocabulary
    from app.services import media_download_service
    from app.services.redaction import config as redaction_config
    from app.services.watch_sources import processing

    assert constants.FUZZY_MATCH_THRESHOLD == 0.85
    assert pipeline_constants.SAMPLE_RATE == 16000
    assert pipeline_constants.MAX_WORD_DURATION == 5.0
    assert pipeline_constants.ASR_SEGMENT_MIN_GAP_SECONDS == 0.5
    assert pipeline_constants.ASR_SEGMENT_MAX_DURATION_SECONDS == 30.0
    assert pipeline_constants.FILES_PER_BATCH_TASK == 25
    assert frozenset({"pii", "profanity", "custom"}) == redaction_config.MASKABLE_CATEGORIES
    assert frozenset(processing.TERMINAL_STATUSES) == {
        "imported",
        "skipped_duplicate",
        "skipped_old",
        "skipped_invalid",
        "stitched_part",
    }
    assert tuple(custom_vocabulary.SUPPORTED_DOMAINS) == (
        "medical",
        "legal",
        "corporate",
        "government",
        "technical",
        "general",
    )
    assert media_download_service.GENERIC_URL_PATTERN.pattern == r"^https?://.+$"
    assert media_download_service.YOUTUBE_URL_PATTERN.match(
        "https://www.youtube.com/playlist?list=PLabc"
    )
