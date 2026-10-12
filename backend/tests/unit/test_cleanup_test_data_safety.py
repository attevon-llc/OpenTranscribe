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
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name in {"_delete_candidates", "_delete_owners"} and self.guard_depth == 0:
                violations.append(f"line {node.lineno}: ungated {name}(...) call")
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

    # The owner-delete dispatch (issue #1237) cascades a whole account, so it is gated too.
    assert _find_ungated_delete_dispatch("def f(api, o):\n    _delete_owners(api, o)\n") != []


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
    # A USER EMAIL prefix, not a media/collection/tag/etc. name — so it is swept by
    # the sibling script `scripts/cleanup-test-users.py`, where it IS registered in
    # `ORPHAN_PATTERNS_UNAMBIGUOUS` as 'litemode-%@example.invalid'. Exempt here
    # because cleanup-test-data.py deliberately owns no user rows, NOT because the
    # prefix is unswept: check that entry rather than taking this line's word for it.
    # Same split as `search_corpus_stack.py`'s `searchqual-` owner, which avoids this
    # allowlist only by spelling its prefix inline instead of naming a constant.
    "backend/tests/integration/test_lite_mode_mocked_providers.py::_LITE_MODE_USER_PREFIX": (
        "litemode-"
    ),
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


# ---------------------------------------------------------------------------------------------
# 7. Store planes (issue #1195): flags, gating, age floor, stale markers, stack scoping
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_planes_default_to_all_and_reject_an_unknown_plane() -> None:
    assert cleanup.build_parser().parse_args([]).planes == cleanup.PLANES
    assert cleanup.build_parser().parse_args(["--planes", "indexes,tmp"]).planes == (
        "indexes",
        "tmp",
    )
    with pytest.raises(SystemExit):
        cleanup.build_parser().parse_args(["--planes", "indexes,everything"])


@pytest.mark.unit
def test_min_age_defaults_to_zero() -> None:
    assert cleanup.build_parser().parse_args([]).min_age_minutes == 0


def _find_ungated_artifact_deletes(source: str) -> list[str]:
    """``_delete_artifacts(...)`` outside an ``effective_execute`` guard, and any plane
    ``.delete_one(...)`` call outside ``_delete_artifacts`` itself."""
    tree = ast.parse(source)
    violations: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "delete_one"
                and fn.name != "_delete_artifacts"
            ):
                violations.append(f"line {node.lineno}: delete_one outside _delete_artifacts")

    class _Visitor(ast.NodeVisitor):
        depth = 0

        def visit_If(self, node: ast.If) -> None:  # noqa: N802
            guarded = any(
                isinstance(n, ast.Name) and n.id == "effective_execute" for n in ast.walk(node.test)
            )
            self.depth += guarded
            for stmt in node.body:
                self.visit(stmt)
            self.depth -= guarded
            for stmt in node.orelse:
                self.visit(stmt)

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name == "_delete_artifacts" and self.depth == 0:
                violations.append(f"line {node.lineno}: ungated _delete_artifacts(...)")
            self.generic_visit(node)

    _Visitor().visit(tree)
    return violations


@pytest.mark.unit
def test_store_deletes_are_reachable_only_behind_the_execute_guard() -> None:
    assert _find_ungated_artifact_deletes(_SCRIPT.read_text(encoding="utf-8")) == []


@pytest.mark.unit
def test_the_store_delete_gate_detector_fires() -> None:
    ungated = "def main(plane, f):\n    _delete_artifacts(plane, [f], 0, include_tier_b=False)\n"
    stray = "def main(plane, f):\n    plane.delete_one(f, 0, include_tier_b=True)\n"
    gated = (
        "def main(effective_execute, plane, f):\n"
        "    if effective_execute and f:\n"
        "        _delete_artifacts(plane, [f], 0, include_tier_b=False)\n"
    )
    assert _find_ungated_artifact_deletes(ungated)
    assert _find_ungated_artifact_deletes(stray)
    assert _find_ungated_artifact_deletes(gated) == []


@pytest.mark.unit
def test_the_age_floor_only_ever_lowers_the_cutoff() -> None:
    assert cleanup.apply_age_floor(1_000_000, 0, now=1_000_000) == 1_000_000
    assert cleanup.apply_age_floor(1_000_000, 30, now=1_000_000) == 1_000_000 - 1800
    # A live run's earlier cutoff already wins.
    assert cleanup.apply_age_floor(500, 30, now=1_000_000) == 500


@pytest.mark.unit
def test_stale_markers_are_pruned_and_live_or_young_ones_kept(tmp_path: Path) -> None:
    import fcntl
    import os
    import time as time_mod

    stale = tmp_path / "stale.lock"
    young = tmp_path / "young.lock"
    live = tmp_path / "live.lock"
    for marker in (stale, young, live):
        marker.write_text("started_at=1\n", encoding="utf-8")
    old = time_mod.time() - 3600
    os.utime(stale, (old, old))
    os.utime(live, (old, old))
    fd = os.open(str(live), os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        dry = cleanup.prune_stale_markers(tmp_path, execute=False, grace_seconds=120)
        assert dry == [stale] and stale.exists(), "a dry run reports and removes nothing"
        removed = cleanup.prune_stale_markers(tmp_path, execute=True, grace_seconds=120)
        assert removed == [stale]
        assert not stale.exists() and young.exists() and live.exists()
    finally:
        os.close(fd)


@pytest.mark.unit
def test_store_planes_refuse_a_mixed_stack_before_touching_anything(tmp_path: Path) -> None:
    """POSTGRES_PORT exported for one stack, OPENSEARCH_PORT for another: exit 2, no I/O.
    Ports point at closed localhost ports, so reaching a store would fail differently."""
    import os
    import subprocess

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.endswith("_PORT") and k not in {"E2E_BACKEND_URL"}
    }
    env.update(
        {
            "POSTGRES_PORT": "5976",
            "OPENSEARCH_PORT": "5180",
            "MINIO_PORT": "5978",
            "TESTRUN_REGISTRY_DIR": str(tmp_path / ".testruns"),
        }
    )
    result = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and the script
        [sys.executable, str(_SCRIPT), "--planes", "indexes"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "REFUSED" in result.stdout and "OPENSEARCH_PORT=5180" in result.stdout


@pytest.mark.unit
def test_orphan_profile_docs_are_the_ones_without_a_row() -> None:
    docs = [
        {"_index": "speakers_v4", "_id": "profile_a", "_source": {"profile_uuid": "a"}},
        {"_index": "speakers_v4", "_id": "profile_b", "_source": {"profile_uuid": "b"}},
        {"_index": "speakers_v4", "_id": "x", "_source": {}},
    ]
    assert cleanup.find_orphan_profile_docs(docs, {"a"}) == ["speakers_v4/profile_b"]
    assert cleanup.find_orphan_profile_docs(docs, {"a", "b"}) == []


# ---------------------------------------------------------------------------------------------
# 8. Speaker profiles: the registered prefix, and the LEGACY shape already leaked on stacks
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "swept"),
    [
        ("e2e-propagated-794e5669", True),
        ("E2E Propagated 794e5669", True),  # legacy: the leftover found on the v060 stack
        ("E2E Propagated notes", False),  # a human's profile with the same words
        ("E2E Propagated 794e5669 copy", False),  # legacy shapes are FULL match
        ("e2e propagated 794e5669", False),
        ("Alice", False),
    ],
)
def test_speaker_profile_shapes(name: str, swept: bool) -> None:
    prefixes = cleanup.NAME_PREFIXES["speaker_profile"]
    assert cleanup._name_matches_shape(name, prefixes, "speaker_profile") is swept


@pytest.mark.unit
def test_legacy_shapes_apply_only_to_their_own_resource() -> None:
    assert not cleanup._name_matches_shape(
        "E2E Propagated 794e5669", cleanup.NAME_PREFIXES["tag"], "tag"
    )


# ---------------------------------------------------------------------------------------------
# 9. e2e literal gate: every uniquified name an e2e test mints must have a registered head
# ---------------------------------------------------------------------------------------------
#
# Section 5 only sees `..._PREFIX = "foo-"` constants. `test_speaker_gender_clusters.py`
# minted f"E2E Propagated {uuid4().hex[:8]}" inline, which no constant scan can see, and a
# profile of that name leaked onto a stack with nothing able to sweep it. This scans every
# f-string in tests/e2e whose value carries a random uniquifier.

_E2E_DIR = _TESTS_DIR / "e2e"


def _is_uniquifier(node: ast.AST, bindings: dict[str, ast.AST]) -> bool:
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        return node.value.attr == "hex"
    if isinstance(node, ast.Attribute):
        return node.attr == "hex"
    if isinstance(node, ast.Call):
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if name in {"uuid4", "token_hex"}:
            return True
        if name == "str" and node.args:
            return _is_uniquifier(node.args[0], bindings)
    if isinstance(node, ast.Name) and node.id in bindings:
        return _is_uniquifier(bindings[node.id], {})
    return False


def _string_constants(source: str) -> dict[str, ast.AST]:
    tree = ast.parse(source)
    return {
        t.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for t in node.targets
        if isinstance(t, ast.Name)
    }


def _shared_e2e_constants() -> dict[str, ast.AST]:
    """Module-level string constants of every e2e module (``OWNED_MEDIA_PREFIX`` lives in
    conftest and is imported by the suites that mint names with it)."""
    shared: dict[str, ast.AST] = {}
    for path in sorted(_E2E_DIR.rglob("*.py")):
        shared.update(_string_constants(path.read_text(encoding="utf-8")))
    return shared


def uniquified_e2e_literals(
    source: str, shared: dict[str, ast.AST] | None = None
) -> list[tuple[int, str, bool]]:
    """``(lineno, head, is_email)``: the constant text before the uniquifier (string
    constants resolved, an unresolvable part rendered as ``{name}``), and whether the
    literal is an email address."""
    tree = ast.parse(source)
    bindings: dict[str, ast.AST] = dict(shared or {})
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                bindings[target.id] = node.value
    found: list[tuple[int, str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        values = node.values
        if not any(
            isinstance(v, ast.FormattedValue) and _is_uniquifier(v.value, bindings) for v in values
        ):
            continue
        head = ""
        for value in values:
            if isinstance(value, ast.Constant):
                head += str(value.value)
                continue
            assert isinstance(value, ast.FormattedValue)
            bound = bindings.get(getattr(value.value, "id", ""))
            if isinstance(bound, ast.Constant) and isinstance(bound.value, str):
                head += bound.value
                continue
            if not _is_uniquifier(value.value, bindings):
                head += "{" + ast.unparse(value.value) + "}"
            break
        tail = "".join(str(v.value) for v in values if isinstance(v, ast.Constant))
        found.append((node.lineno, head, "@" in tail))
    return found


def _email_heads() -> list[str]:
    users = cleanup._load_cleanup_users_module()
    return [p.split("%", 1)[0].replace("\\_", "_") for p in users.ORPHAN_PATTERNS]


def _head_registered(head: str, is_email: bool) -> bool:
    if not head or head.startswith("{"):
        return False
    if is_email:
        return any(head.startswith(h) and h for h in _email_heads())
    return any(head.startswith(p) for p in _registered_name_prefixes())


#: Uniquified e2e literals that name nothing persistent, keyed ``file::head``.
_E2E_LITERAL_NOT_PERSISTED: dict[str, str] = {
    "backend/tests/e2e/conftest.py::Zq3!": "a password value, not a name",
    "backend/tests/e2e/test_support_access.py::Zq3!": "a password value, not a name",
    "backend/tests/e2e/test_support_access.py::INC-": (
        "an incident reference typed into the support-access grant; stored on the grant row, "
        "which is removed with the support-e2e- subject account (cleanup-test-users.py)"
    ),
    "backend/tests/e2e/test_support_access.py::Support Subject ": (
        "the display name of the support-e2e- account (email swept by cleanup-test-users.py)"
    ),
    "backend/tests/e2e/test_full_pipeline_smoke.py::Pipeline smoke vLLM ": (
        "an LLM config name; leaked configs are swept by base_url (llm-test-vllm) in "
        "cleanup-test-users.py, not by name"
    ),
    "backend/tests/e2e/test_chat_trace_panel.py::gh514-trace-e2e-": (
        "an LLM config name; swept by base_url (mock-llm) in cleanup-test-users.py"
    ),
    "backend/tests/e2e/test_visual_regression.py::gh514-visual-": (
        "an LLM config name; swept by base_url (mock-llm) in cleanup-test-users.py"
    ),
    "backend/tests/e2e/test_chat.py::{label}": (
        "_unique() names LLM provider configs (swept by base_url in cleanup-test-users.py); "
        "conversation titles use unique_conversation_title() and its e2e-chat- prefix"
    ),
    "backend/tests/e2e/test_chat_grounding.py::{name}": (
        "llm_config_factory's LLM config name; swept by base_url in cleanup-test-users.py"
    ),
    "backend/tests/e2e/test_watch_sources_e2e.py::noreply+": (
        "the from_address of the e2e-watch-mailer- email config, not an account; the "
        "config row is deleted in the test's finally"
    ),
    "backend/tests/e2e/test_registration.py::unique_": (
        "registration form username field for a unique_%@example.com account (review tier)"
    ),
    "backend/tests/e2e/test_registration.py::testuser_": (
        "registration form username for a testuser_%@example.com account (review tier)"
    ),
    "backend/tests/e2e/test_registration.py::newuser_": (
        "registration form username for a newuser_%@example.com account (review tier)"
    ),
}


def _e2e_offenders() -> tuple[dict[str, str], int]:
    offenders: dict[str, str] = {}
    scanned = 0
    shared = _shared_e2e_constants()
    for path in sorted(_E2E_DIR.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT)
        source = path.read_text(encoding="utf-8")
        for lineno, head, is_email in uniquified_e2e_literals(source, shared):
            scanned += 1
            if _head_registered(head, is_email) or f"{rel}::{head}" in _E2E_LITERAL_NOT_PERSISTED:
                continue
            offenders[f"{rel}:{lineno}"] = repr(head)
    return offenders, scanned


@pytest.mark.unit
def test_every_uniquified_e2e_name_has_a_registered_head() -> None:
    offenders, scanned = _e2e_offenders()
    assert scanned > 10, f"scanned only {scanned} literals: the scanner stopped matching"
    assert offenders == {}, (
        "e2e literals minting a uniquified name whose head no sweep recognises "
        "(register the prefix in scripts/cleanup-test-data.py NAME_PREFIXES/"
        "MEDIA_FILENAME_SPECS or cleanup-test-users.py ORPHAN_PATTERNS, or add a "
        f"_E2E_LITERAL_NOT_PERSISTED entry with a reason): {offenders}"
    )


@pytest.mark.unit
def test_every_e2e_literal_allowlist_entry_is_still_used() -> None:
    used = set()
    shared = _shared_e2e_constants()
    for path in sorted(_E2E_DIR.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT)
        for _, head, _ in uniquified_e2e_literals(path.read_text(encoding="utf-8"), shared):
            used.add(f"{rel}::{head}")
    stale = sorted(set(_E2E_LITERAL_NOT_PERSISTED) - used)
    assert stale == [], f"stale _E2E_LITERAL_NOT_PERSISTED entries: {stale}"


@pytest.mark.unit
def test_the_e2e_literal_scanner_fires_and_stays_clean() -> None:
    leaky = "import uuid\nnew_name = f'E2E Propagated {uuid.uuid4().hex[:8]}'\n"
    [(_, head, is_email)] = uniquified_e2e_literals(leaky)
    assert head == "E2E Propagated " and not is_email
    assert not _head_registered(head, is_email)

    via_constant = (
        "import uuid\nTAG_PREFIX = 'e2e-tag-'\nname = f'{TAG_PREFIX}{uuid.uuid4().hex[:8]}'\n"
    )
    [(_, head, is_email)] = uniquified_e2e_literals(via_constant)
    assert head == "e2e-tag-" and _head_registered(head, is_email)

    email = "import uuid\ne = f'mfa-e2e-{uuid.uuid4().hex[:8]}@example.com'\n"
    [(_, head, is_email)] = uniquified_e2e_literals(email)
    assert is_email and _head_registered(head, is_email)
    unregistered_email = "import uuid\ne = f'brand-new-{uuid.uuid4().hex[:8]}@example.com'\n"
    [(_, head, is_email)] = uniquified_e2e_literals(unregistered_email)
    assert not _head_registered(head, is_email)

    no_uniquifier = "x = 5\nmsg = f'count {x}'\n"
    assert uniquified_e2e_literals(no_uniquifier) == []
