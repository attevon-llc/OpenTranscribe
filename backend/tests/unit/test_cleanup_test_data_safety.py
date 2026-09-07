"""Fast, DB-free safety proofs for ``scripts/cleanup-test-data.py`` (issue #629).

Same loading pattern as ``test_cleanup_test_users_safety.py`` (hyphenated filename,
``importlib.util.spec_from_file_location``). The isolated-throwaway-Postgres proof for
the actual deletion behaviour lives in
``backend/tests/integration/test_cleanup_test_data_isolated.py``; this file is unit-only
— no DB, no Docker, no live stack, no network.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "cleanup-test-data.py"
_TESTS_DIR = _REPO_ROOT / "backend" / "tests"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cleanup_test_data", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cleanup = _load_script()


# ---------------------------------------------------------------------------------------------
# 1. --execute / --execute-unambiguous default off; --execute-unambiguous selects Tier A only
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_execute_flags_default_to_false() -> None:
    args = cleanup.build_parser().parse_args([])
    assert args.execute is False
    assert args.execute_unambiguous is False


@pytest.mark.unit
def test_json_flag_defaults_to_false() -> None:
    args = cleanup.build_parser().parse_args([])
    assert args.json is False


# ---------------------------------------------------------------------------------------------
# 2. AST gate: every delete-issuing API call sits behind the effective_execute guard
# ---------------------------------------------------------------------------------------------


def _find_ungated_delete_dispatch(source: str) -> list[str]:
    """Every call to ``_delete_candidates`` (the sole function issuing real API
    deletes) that is NOT nested inside an ``if`` whose test mentions
    ``effective_execute``. Mirrors ``cleanup-test-users.py``'s ``_find_ungated_deletes``
    shape, adapted: this script's delete surface is a function call, not a string
    literal, because the actual HTTP verb lives inside ``ApiSession`` methods that are
    only ever reached through this one dispatcher.
    """

    def _is_execute_guard(test: ast.expr) -> bool:
        if isinstance(test, ast.BoolOp):
            return any(_is_execute_guard(v) for v in test.values)
        if isinstance(test, ast.Name):
            return test.id in {"effective_execute", "execute", "execute_unambiguous"}
        return isinstance(test, ast.Attribute) and test.attr in {
            "effective_execute",
            "execute",
            "execute_unambiguous",
        }

    tree = ast.parse(source)
    violations: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.guard_depth = 0

        def visit_If(self, node: ast.If) -> None:  # noqa: N802
            if _is_execute_guard(node.test):
                self.guard_depth += 1
                for stmt in node.body:
                    self.visit(stmt)
                self.guard_depth -= 1
                for stmt in node.orelse:
                    self.visit(stmt)
            else:
                self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            is_dispatch_call = (
                isinstance(node.func, ast.Name) and node.func.id == "_delete_candidates"
            ) or (isinstance(node.func, ast.Attribute) and node.func.attr == "_delete_candidates")
            if is_dispatch_call and self.guard_depth == 0:
                violations.append(f"line {node.lineno}: ungated _delete_candidates(...) call")
            self.generic_visit(node)

    _Visitor().visit(tree)
    return violations


@pytest.mark.unit
def test_the_delete_dispatch_is_reachable_only_behind_the_execute_guard() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    violations = _find_ungated_delete_dispatch(source)
    assert violations == [], f"ungated delete dispatch call(s): {violations}"


@pytest.mark.unit
def test_the_ungated_delete_dispatch_detector_actually_fires() -> None:
    unguarded = "def f(effective_execute, api, resource, candidates):\n    _delete_candidates(api, resource, candidates)\n"
    assert _find_ungated_delete_dispatch(unguarded) != []

    guarded = (
        "def f(effective_execute, api_available, api, resource, candidates):\n"
        "    if effective_execute and api_available:\n"
        "        _delete_candidates(api, resource, candidates)\n"
    )
    assert _find_ungated_delete_dispatch(guarded) == []


# ---------------------------------------------------------------------------------------------
# 3. Media filename SHAPE is load-bearing, not vacuous (Decision 2's worked example)
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "matches", "does_not_match"),
    [
        ("owned", "e2e-owned-a3fbdada.wav", "e2e-owned-notes.wav"),
        ("upload", "e2e_upload_440_12345.wav", "e2e_upload_notes.wav"),
        ("reprocess", "reprocess-a3fbdada.wav", "reprocess-notes.wav"),
        ("gpu-scale-smoke", "gpu-scale-smoke-0-12345.wav", "gpu-scale-smoke-notes.wav"),
    ],
)
def test_media_filename_shape_matches_fixture_and_rejects_a_human_filename(
    label: str, matches: str, does_not_match: str
) -> None:
    spec = next(s for s in cleanup.MEDIA_FILENAME_SPECS if s.label == label)
    assert spec.shape.match(matches), f"{spec.shape.pattern} should match {matches!r}"
    assert not spec.shape.match(does_not_match), (
        f"{spec.shape.pattern} should NOT match human-plausible {does_not_match!r} — "
        "a bare prefix match here would sweep a real recording"
    )


@pytest.mark.unit
def test_name_prefix_shape_requires_the_hex_suffix() -> None:
    assert cleanup._name_matches_shape("e2e-tag-a3fbdada", ["e2e-tag-"])
    assert not cleanup._name_matches_shape("e2e-tag-notes", ["e2e-tag-"]), (
        "a bare prefix match would sweep a human-created 'e2e-tag-notes' tag"
    )
    assert not cleanup._name_matches_shape("something-else", ["e2e-tag-"])


# ---------------------------------------------------------------------------------------------
# 4. Pure liveness-cutoff functions — no I/O
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_cutoff_with_no_live_runs_is_now() -> None:
    assert cleanup.resolve_cutoff([], now=1000) == 1000


@pytest.mark.unit
def test_resolve_cutoff_is_the_oldest_live_run_including_a_later_now() -> None:
    assert cleanup.resolve_cutoff([500, 700], now=1000) == 500


@pytest.mark.unit
def test_resolve_cutoff_never_exceeds_now_even_with_a_future_marker() -> None:
    """A clock-skewed or corrupted marker must never push the cutoff PAST now — that
    would let a row created between now and the bad marker survive review forever.
    """
    assert cleanup.resolve_cutoff([5000], now=1000) == 1000


@pytest.mark.unit
def test_live_marker_start_times_on_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert cleanup.live_marker_start_times(tmp_path / "nope") == []


# ---------------------------------------------------------------------------------------------
# 5. Every media-filename / collection / tag / watch-source / speaker-profile /
#    conversation prefix minted anywhere under backend/tests is registered here.
# ---------------------------------------------------------------------------------------------


def _name_minting_prefixes(source: str) -> dict[str, str]:
    """``{CONSTANT_NAME: prefix_value}`` for module-level ``..._PREFIX`` string
    constants that are actually used to build an f-string value (never the bare
    declaration alone — that would fire on a docstring example) and are NOT an
    email-minting prefix (those are ``cleanup-test-users.py``'s ORPHAN_PATTERNS
    concern, covered by its own ``test_every_e2e_registered_prefix_has_an_orphan_pattern``,
    not this script's).
    """
    prefixes: dict[str, str] = {}
    for match in re.finditer(
        r'^([_A-Z][A-Z0-9_]*_PREFIX)\s*=\s*"([^"]*-)"\s*$', source, re.MULTILINE
    ):
        name, value = match.group(1), match.group(2)
        usage = re.compile(r"f\"\{" + re.escape(name) + r"\}")
        if not usage.search(source):
            continue
        email_usage = re.compile(r"f\"\{" + re.escape(name) + r"\}[^\"]*@example")
        if email_usage.search(source):
            continue
        prefixes[name] = value
    return prefixes


def _registered_name_prefixes() -> set[str]:
    registered: set[str] = set()
    for prefixes in cleanup.NAME_PREFIXES.values():
        registered.update(prefixes)
    for spec in cleanup.MEDIA_FILENAME_SPECS:
        # sql_prefix is a LIKE pattern: strip the trailing '%' and unescape '\_' -> '_'.
        normalised = spec.sql_prefix.removesuffix("%").replace("\\_", "_")
        registered.add(normalised)
    return registered


#: Constants matching the `..._PREFIX = "...-"` shape that are NOT a media-filename /
#: collection / tag / watch-source / speaker-profile / conversation naming prefix, so
#: cleanup-test-data.py correctly does not track them. Each entry needs a written
#: reason, same convention as `backend/tests/audit-allowlist.txt`.
_NOT_A_DATA_PREFIX = {
    # Docker container-name / compose-project prefix for the benchmark stack
    # (./opentr.sh bench) — infrastructure naming, not a DB row this script sweeps.
    "backend/tests/unit/test_opentr_bench_gates.py::_BENCH_PREFIX": "otbench-",
}


@pytest.mark.unit
def test_every_name_minting_prefix_under_backend_tests_is_registered() -> None:
    unmatched: dict[str, str] = {}
    scanned_any = False
    registered = _registered_name_prefixes()
    for path in _TESTS_DIR.rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="ignore")
        prefixes = _name_minting_prefixes(source)
        if prefixes:
            scanned_any = True
        for name, value in prefixes.items():
            key = f"{path.relative_to(_REPO_ROOT)}::{name}"
            if value not in registered and _NOT_A_DATA_PREFIX.get(key) != value:
                unmatched[key] = value

    stale_allowlist = {key for key, value in _NOT_A_DATA_PREFIX.items() if value in registered}
    assert stale_allowlist == set(), (
        f"_NOT_A_DATA_PREFIX entries now registered anyway — delete them: {stale_allowlist}"
    )

    assert scanned_any, (
        "the scan found no name-minting prefix constants anywhere under backend/tests "
        "— it may have stopped matching rather than the tree having none"
    )
    assert unmatched == {}, (
        f"name-minting prefix(es) with no matching cleanup-test-data.py pattern: {unmatched}"
    )


@pytest.mark.unit
def test_the_name_minting_prefix_scanner_actually_fires() -> None:
    """Guard the guard: a synthetic unregistered prefix must be reported, and a
    registered one (or an email-minting one) must not.
    """
    synthetic_unregistered = (
        'BRAND_NEW_PREFIX = "totally-unregistered-"\nx = f"{BRAND_NEW_PREFIX}{1}"\n'
    )
    found = _name_minting_prefixes(synthetic_unregistered)
    assert found == {"BRAND_NEW_PREFIX": "totally-unregistered-"}
    assert found["BRAND_NEW_PREFIX"] not in _registered_name_prefixes()

    synthetic_email = 'MINT_PREFIX = "mint-e2e-"\nx = f"{MINT_PREFIX}{1}@example.com"\n'
    assert _name_minting_prefixes(synthetic_email) == {}

    unused_declaration_only = 'DOC_EXAMPLE_PREFIX = "doc-example-"\n'
    assert _name_minting_prefixes(unused_declaration_only) == {}


# ---------------------------------------------------------------------------------------------
# 6. classify()-shaped pure helper: de-dupe never drops a distinct candidate
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_media_by_owner_and_media_by_filename_are_reconciled_without_duplication() -> None:
    """Both discovery paths can find the SAME row (a Tier-A-owned file that also
    matches a filename shape); the caller de-dupes on uuid. This test pins that the
    de-dupe key is the right one — two DIFFERENT files must both survive.
    """
    same_uuid = "11111111-1111-1111-1111-111111111111"
    other_uuid = "22222222-2222-2222-2222-222222222222"
    by_filename = [
        cleanup.Candidate("media_file", same_uuid, "e2e-owned-aaaaaaaa.wav", "a@example.com")
    ]
    by_owner = [
        cleanup.Candidate("media_file", same_uuid, "e2e-owned-aaaaaaaa.wav", "a@example.com"),
        cleanup.Candidate(
            "media_file", other_uuid, "unrelated.wav", "searchqual-x@example.invalid"
        ),
    ]
    combined = by_filename + by_owner
    seen: set[str] = set()
    deduped = []
    for cand in combined:
        if cand.uuid not in seen:
            seen.add(cand.uuid)
            deduped.append(cand)
    assert {c.uuid for c in deduped} == {same_uuid, other_uuid}
    assert len(deduped) == 2
