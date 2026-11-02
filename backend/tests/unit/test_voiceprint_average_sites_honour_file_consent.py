"""Every place that AVERAGES voiceprints must exclude opted-out files (issue #1356, risk 1).

If one averaging path is missed, an opted-out voice survives inside a profile or cluster average.
The sites are DERIVED from the code, not listed by hand: every module under ``app/`` that calls
``np.mean`` / ``np.average`` / ``aggregate_embeddings`` and deals in embeddings. A module passes
by naming ``declined_file_ids`` (the one exclusion rule), or by sitting in ``AVERAGES_ONE_FILE``
/ ``NOT_VOICEPRINT_AVERAGING`` with a written reason. A new averaging module therefore fails here
until somebody decides which it is.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
_AVERAGERS = {"mean", "average"}
_HELPER = "declined_file_ids"

#: Averages the segments of ONE speaker within ONE file. That file's consent is enforced before
#: it runs (``voiceprints_allowed_for_file`` gate at the pipeline entry, plus the file-aware
#: write backstop), so there is no cross-file average to exclude from.
AVERAGES_ONE_FILE = {
    "tasks/embedding_consistency_repair.py": "per-speaker segment mean inside one file; its "
    "writers call _voiceprints_allowed(file) first",
    "tasks/transcription/embeddings.py": "per-speaker mean of one file's diarizer centroids; "
    "gated by voiceprints_allowed(file_id) at the stage entry",
    "services/speaker_embedding_service.py": "aggregate_embeddings over one speaker's segments",
    "services/speaker_matching_service.py": "per-speaker aggregate of one file's embeddings; "
    "gated by the file-level policy before it stores",
    "utils/embedding_helpers.py": "pure vector helper; callers own the file scope",
    "services/native_embedding_client.py": "tiles ONE audio clip's windows into one vector",
    "services/voiceprint_extractor.py": "per-speaker vector from one file's audio",
}

#: Averages something that is not a stored voiceprint, or reaches one only through a caller that
#: excludes. Empty today; kept as the place a reason goes rather than a silent skip.
NOT_VOICEPRINT_AVERAGING: dict[str, str] = {
    "api/endpoints/speakers.py": "_merge_speaker_embeddings averages two speakers; its only "
    "caller, tasks/speaker_merge_task.process_speaker_merge_background, skips it when either "
    "file is declined (asserted below)",
}


def _averaging_modules() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(APP.rglob("*.py")):
        text = path.read_text()
        if "embedding" not in text and "voiceprint" not in text:
            continue
        tree = ast.parse(text)
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr in _AVERAGERS)
                or (isinstance(node.func, ast.Name) and node.func.id == "aggregate_embeddings")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "aggregate_embeddings"
                )
            )
        ]
        if lines:
            found[path.relative_to(APP).as_posix()] = lines
    return found


def _names_helper(rel: str) -> bool:
    return _HELPER in ast.dump(ast.parse((APP / rel).read_text()))


def test_the_scan_finds_the_known_averagers():
    found = _averaging_modules()

    assert "services/profile_embedding_service.py" in found
    assert "services/speaker_clustering_service.py" in found
    assert "tasks/embedding_migration_v4.py" in found
    assert "api/endpoints/speakers.py" in found


def test_every_cross_file_averager_names_the_exclusion_rule():
    unclassified = []
    for rel in _averaging_modules():
        if rel in AVERAGES_ONE_FILE or rel in NOT_VOICEPRINT_AVERAGING:
            continue
        if not _names_helper(rel):
            unclassified.append(rel)

    assert unclassified == [], (
        "These modules average embeddings but neither use declined_file_ids nor are classified "
        f"with a reason in this test: {unclassified}"
    )


def test_the_known_cross_file_averagers_really_use_it():
    for rel in (
        "services/profile_embedding_service.py",
        "services/speaker_clustering_service.py",
        "tasks/embedding_migration_v4.py",
        "tasks/speaker_merge_task.py",  # the only caller of the speakers.py merge average
    ):
        assert _names_helper(rel), rel


def test_the_classification_tables_hold_no_stale_entries():
    found = _averaging_modules()
    stale = [
        rel
        for rel in (*AVERAGES_ONE_FILE, *NOT_VOICEPRINT_AVERAGING)
        if rel not in found and not (APP / rel).exists()
    ]

    assert stale == []


@pytest.mark.parametrize("table", [AVERAGES_ONE_FILE, NOT_VOICEPRINT_AVERAGING])
def test_every_classification_has_a_reason(table):
    assert all(len(reason.strip()) > 10 for reason in table.values())


def test_a_module_in_a_table_that_no_longer_averages_is_reported_so_the_table_shrinks():
    found = _averaging_modules()
    never_averages = [
        rel for rel in (*AVERAGES_ONE_FILE, *NOT_VOICEPRINT_AVERAGING) if rel not in found
    ]

    assert never_averages == [], f"remove from the tables: {never_averages}"
