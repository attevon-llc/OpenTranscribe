"""Every kNN query body excludes trashed and quarantined files' voices (#1101, #1253).

A trashed or taken-down file keeps its per-file speaker documents, stamped
``availability: trashed`` / ``quarantined``. Speaker matching, similarity, clustering and
profile suggestions are all kNN reads over those documents, and such a voice that still matched
would put its speaker back into suggestions and clusters. The rule: a function that builds a kNN
body (a dict with a ``"knn"`` key whose value is a query, not the ``"knn": True`` index
setting) calls ``availability_field.exclude_unavailable_clause()`` WITHOUT the
``include_quarantined`` review exemption: matching and suggestions are not review surfaces, so a
taken-down voice is excluded for every caller.

The sweep is over every module under ``app/`` rather than a list of known sites, because the
failure it guards is a NEW kNN reader. Exemptions are allowlisted with a reason, and an entry
whose function no longer builds a kNN body fails the run. Self-tested below: the scanner must
fire on a body without the helper and stay quiet on one with it, or an empty result would be
indistinguishable from a clean tree.

Profile and cluster bodies carry the clause too although their documents name no file: one
rule with no judgement call is what keeps a later reader from deciding wrongly.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
HELPER = "exclude_unavailable_clause"

#: ``<path under app/>::<function>`` -> why its kNN body must not exclude the trash.
ALLOWED: dict[str, str] = {
    "services/opensearch_service/client.py::probe_knn_health": (
        "A k=1 health probe with size 0 and no _source: it asks whether the HNSW graph can be "
        "traversed at all and returns no document to anyone."
    ),
}


def _is_knn_body(node: ast.Dict) -> bool:
    return any(
        isinstance(key, ast.Constant) and key.value == "knn" and isinstance(value, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
    )


def _calls_helper(func: ast.AST) -> bool:
    """Whether ``func`` calls the helper with no quarantine exemption on any of its calls."""
    calls = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and (node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None))
        == HELPER
    ]
    return bool(calls) and not any(
        kw.arg == "include_quarantined" for call in calls for kw in call.keywords
    )


def scan(source: str, relpath: str) -> dict[str, bool]:
    """``{"<relpath>::<outermost function>": calls_helper}`` for each function building a kNN body."""
    found: dict[str, bool] = {}
    tree = ast.parse(source)
    for top in ast.walk(tree):
        if not isinstance(top, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if any(isinstance(n, ast.Dict) and _is_knn_body(n) for n in ast.walk(top)):
            key = f"{relpath}::{top.name}"
            # Nested functions are walked twice; the outermost answer (a superset) wins.
            found[key] = found.get(key, False) or _calls_helper(top)
    return found


def _all_sites() -> dict[str, bool]:
    sites: dict[str, bool] = {}
    for path in sorted(APP.rglob("*.py")):
        source = path.read_text()
        if '"knn"' not in source:
            continue
        sites.update(scan(source, path.relative_to(APP).as_posix()))
    return sites


def test_every_knn_body_excludes_the_trash():
    offenders = sorted(k for k, ok in _all_sites().items() if not ok and k not in ALLOWED)
    assert not offenders, (
        "These functions build a kNN query without "
        "availability_field.exclude_unavailable_clause(), or pass it the quarantine exemption, "
        "so a trashed or taken-down file's voices still match. Add the clause (no exemption) to "
        "the knn filter, or allowlist the function with a reason:\n  " + "\n  ".join(offenders)
    )


def test_the_known_knn_readers_are_all_found():
    """The sweep must see the nine speaker-plane kNN readers, or it is checking nothing."""
    sites = _all_sites()
    for expected in (
        "services/opensearch_service/matching.py::find_matching_speaker",
        "services/opensearch_service/matching.py::batch_find_matching_speakers",
        "services/opensearch_service/matching.py::msearch_speaker_similarities",
        "services/opensearch_service/profiles.py::msearch_profile_knn_batch",
        "services/opensearch_service/profiles.py::find_matching_profiles",
        "services/opensearch_service/clusters.py::find_matching_clusters",
        "services/similarity_service.py::opensearch_similarity_search",
        "services/speaker_matching/match_storage.py::build_speaker_match_query",
        "services/profile_embedding_service.py::_execute_knn_search",
    ):
        assert expected in sites, (expected, sorted(sites))


def test_every_allowlist_entry_still_builds_a_knn_body():
    stale = sorted(k for k in ALLOWED if k not in _all_sites())
    assert not stale, f"stale allowlist entries (delete them): {stale}"


# --- self-test -------------------------------------------------------------------------------


def test_scanner_fires_on_a_knn_body_without_the_helper():
    src = (
        "def find(v):\n"
        "    q = {'query': {'knn': {'embedding': {'vector': v, 'k': 5,\n"
        "        'filter': {'bool': {'filter': [{'term': {'user_id': 1}}]}}}}}}\n"
        "    return q\n"
    )
    assert scan(src, "x.py") == {"x.py::find": False}


def test_scanner_fires_on_a_knn_body_that_exempts_the_quarantine():
    src = (
        "def find(v, admin):\n"
        "    fs = [exclude_unavailable_clause(include_quarantined=admin)]\n"
        "    return {'query': {'knn': {'embedding': {'vector': v, 'filter': {'bool': {'filter': fs}}}}}}\n"
    )
    assert scan(src, "x.py") == {"x.py::find": False}


def test_scanner_stays_clean_on_a_body_with_the_helper_and_on_the_index_setting():
    src = (
        "def find(v):\n"
        "    fs = [exclude_unavailable_clause()]\n"
        "    return {'query': {'knn': {'embedding': {'vector': v, 'filter': {'bool': {'filter': fs}}}}}}\n"
        "def create():\n"
        "    return {'settings': {'index': {'knn': True}}}\n"
    )
    assert scan(src, "x.py") == {"x.py::find": True}
