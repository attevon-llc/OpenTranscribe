"""Safety proofs for the store planes of ``scripts/cleanup-test-data.py`` (issue #1195).

``scripts/lib/test_artifact_sweep.py`` decides which OpenSearch indexes, MinIO buckets and
objects, Postgres databases, ``/tmp`` entries and docker resources a sweep may delete. Every
decision is a pure function, so it is proved here without touching a store: the must-refuse
cases (production names, unregistered names, possibly-live artifacts, mixed-stack ports) are
the point of this file; the must-select cases prove the refusals are not vacuous. The real
round trip against live stores is ``integration/test_test_artifact_sweep_live.py``.

The last section is the REGISTRATION gate: every throwaway store a test under
``backend/tests`` creates must have a registered shape, or a killed run's leftover of it is
invisible to the sweep forever.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import stat
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _REPO_ROOT / "scripts" / "lib" / "test_artifact_sweep.py"
_TESTS_DIR = _REPO_ROOT / "backend" / "tests"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("test_artifact_sweep", _MODULE)
    assert spec is not None and spec.loader is not None, f"cannot load {_MODULE}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sweep = _load()

#: A cutoff far in the future: everything "old enough". Liveness is tested separately.
_LATE = 4_000_000_000
_OLD = 1_000_000_000.0


# ---------------------------------------------------------------------------------------------
# 1. Indexes
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "transcript_chunks",
        "transcripts",
        "speakers",
        "speakers_v3",
        "speakers_v4",
        "speakers_v3_backup",
        "audit-logs-2026.10",
        "top_queries-2026.10.11-59491",
        ".plugins-ml-model",
        ".tasks",
        ".opendistro-job-scheduler-lock",
        "transcript_chunks_v2",
    ],
)
def test_a_production_index_is_refused_even_if_a_shape_would_match_it(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The protected list is checked BEFORE the registry, so a too-broad shape cannot
    reach a production index. Proved with a shape that matches every name."""
    match_all = sweep.ArtifactSpec("everything", re.compile(r".*"), sweep.TIER_A, "test")
    monkeypatch.setattr(sweep, "TEST_INDEX_SPECS", [match_all])
    finding = sweep.classify_index(name, 0, _OLD, _LATE)
    assert finding.tier is None
    assert "protected" in finding.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "test_summary_api_notes",  # registered prefix, no uniquifier: a human's index
        "test_summary_api_0123456789ab-x",  # trailing junk: FULL match required
        "test_summary_api_0123456789a",  # 11 hex, fixture mints 12
        "my_experiment",
        "test_something_else_0123456789ab",
    ],
)
def test_an_unregistered_index_name_is_refused(name: str) -> None:
    finding = sweep.classify_index(name, 0, _OLD, _LATE)
    assert finding.tier is None
    assert "no registered test shape" in finding.reason


@pytest.mark.unit
def test_an_empty_registered_index_is_tier_a_and_a_populated_one_tier_b() -> None:
    empty = sweep.classify_index("test_summary_api_0123456789ab", 0, _OLD, _LATE)
    full = sweep.classify_index("test_summary_api_0123456789ab", 3, _OLD, _LATE)
    assert empty.tier == sweep.TIER_A
    assert full.tier == sweep.TIER_B
    assert sweep.selected(empty, include_tier_b=False)
    assert not sweep.selected(full, include_tier_b=False), "documents need --execute"
    assert sweep.selected(full, include_tier_b=True)


@pytest.mark.unit
def test_an_index_created_after_the_cutoff_may_be_live_and_is_refused() -> None:
    cutoff = 2_000_000_000
    fresh = sweep.classify_index("test_summary_api_0123456789ab", 0, cutoff - 1, cutoff)
    inside_grace = sweep.classify_index(
        "test_summary_api_0123456789ab", 0, cutoff - sweep.GRACE_SECONDS + 1, cutoff
    )
    old = sweep.classify_index("test_summary_api_0123456789ab", 0, cutoff - 3600, cutoff)
    assert fresh.tier is None and inside_grace.tier is None
    assert old.tier == sweep.TIER_A


@pytest.mark.unit
def test_an_index_with_unknown_age_or_count_is_refused() -> None:
    assert sweep.classify_index("test_summary_api_0123456789ab", 0, None, _LATE).tier is None
    assert sweep.classify_index("test_summary_api_0123456789ab", None, _OLD, _LATE).tier is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "test_summary_api_0123456789ab",
        "test_transcripts_tenancy_d7a44354be",
        "test_label_drift_0123456789ab",
        "test_vp_erase_0123456789_v3_backup",
        "test_speakers_maint_v3_0123456789_v4",
        "knn_probe_live_test_non_ann_0123456789",
        "delres-negctl-0123456789ab",
        "test_priv_chunks_0123456789ab",
        "test_priv_spk_0123456789ab",
        "test_priv_spk_0123456789ab_v3",
        "test_priv_spk_0123456789ab_v4",
        "test_priv_spk_0123456789ab_v3_backup",
    ],
)
def test_real_fixture_index_names_are_registered(name: str) -> None:
    """One per family, including the leftover actually found on the isolated stack
    (``test_transcripts_tenancy_d7a44354be``)."""
    assert sweep.classify_index(name, 0, _OLD, _LATE).tier == sweep.TIER_A


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "transcript_chunks",
        "speakers_v4",
        "test_priv_chunks_notes",
        "test_priv_chunks_0123456789ab_extra",
        "test_priv_spk_0123456789ab_v5",
        "test_priv_spk_short_v3",
    ],
)
def test_production_and_near_miss_index_names_are_not_privacy_shapes(name: str) -> None:
    assert sweep.classify_index(name, 0, _OLD, _LATE).tier is None


@pytest.mark.unit
def test_a_registered_pipeline_is_a_candidate_and_any_other_is_not() -> None:
    assert sweep.classify_pipeline("test-completeness-ingest-0123abcd").tier == sweep.TIER_A
    assert sweep.classify_pipeline("transcript-ingest-pipeline").tier is None
    assert sweep.classify_pipeline("test-completeness-ingest-notes").tier is None


# ---------------------------------------------------------------------------------------------
# 2. Databases
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", ["postgres", "template0", "template1", "opentranscribe"])
def test_a_system_or_default_database_is_refused(monkeypatch: pytest.MonkeyPatch, name: str):
    match_all = sweep.ArtifactSpec("everything", re.compile(r".*"), sweep.TIER_A, "test")
    monkeypatch.setattr(sweep, "TEST_DATABASE_SPECS", [match_all])
    assert sweep.classify_database(name, 0, set()).tier is None


@pytest.mark.unit
def test_the_target_database_is_refused_whatever_its_name(monkeypatch: pytest.MonkeyPatch):
    """A deployment whose real database is called ``ot_prod_test`` must not be swept."""
    finding = sweep.classify_database("ot_prod_test", 0, {"ot_prod_test"})
    assert finding.tier is None and "protected" in finding.reason


@pytest.mark.unit
def test_a_scratch_database_is_tier_b_and_never_while_connected() -> None:
    idle = sweep.classify_database("ot_perfile_test", 0, {"opentranscribe"})
    busy = sweep.classify_database("ot_perfile_test", 2, {"opentranscribe"})
    assert idle.tier == sweep.TIER_B, "hand-made scratch databases need --execute"
    assert busy.tier is None and "connection" in busy.reason


@pytest.mark.unit
@pytest.mark.parametrize("name", ["opentranscribe_test", "ot_test", "ot_perfile_test2", "x_test"])
def test_a_database_outside_the_scratch_shape_is_refused(name: str) -> None:
    assert sweep.classify_database(name, 0, set()).tier is None


# ---------------------------------------------------------------------------------------------
# 3. Buckets and objects
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", ["opentranscribe", "transcribe-app", "my-media"])
def test_a_configured_or_default_bucket_is_refused(monkeypatch: pytest.MonkeyPatch, name: str):
    match_all = sweep.ArtifactSpec("everything", re.compile(r".*"), sweep.TIER_A, "test")
    monkeypatch.setattr(sweep, "TEST_BUCKET_SPECS", [match_all])
    finding = sweep.classify_bucket(name, _OLD, _LATE, {"my-media"})
    assert finding.tier is None and "protected" in finding.reason


@pytest.mark.unit
def test_a_test_bucket_is_tier_a_only_when_old_enough() -> None:
    assert sweep.classify_bucket("test-erasure-journal-0123456789", _OLD, _LATE, set()).tier == "A"
    young = sweep.classify_bucket("test-erasure-journal-0123456789", _LATE - 1, _LATE, set())
    assert young.tier is None
    assert sweep.classify_bucket("test-erasure-journal-x", _OLD, _LATE, set()).tier is None


@pytest.mark.unit
def test_a_test_object_must_match_both_its_bucket_and_its_full_key() -> None:
    key = "tests/presign-revocation-907/" + "a" * 32 + ".txt"
    assert sweep.classify_object("media", key, "MEDIA_BUCKET_NAME", _OLD, _LATE).tier == "A"
    # Same key in the cache bucket is not what the test writes.
    assert sweep.classify_object("cache", key, "CACHE_BUCKET_NAME", _OLD, _LATE).tier is None
    # A real upload's key never matches.
    real = "user_1/file_2/recording.wav"
    assert sweep.classify_object("media", real, "MEDIA_BUCKET_NAME", _OLD, _LATE).tier is None
    # Prefix alone is not enough.
    loose = "tests/presign-revocation-907/notes.txt"
    assert sweep.classify_object("media", loose, "MEDIA_BUCKET_NAME", _OLD, _LATE).tier is None
    young = sweep.classify_object("media", key, "MEDIA_BUCKET_NAME", _LATE - 1, _LATE)
    assert young.tier is None


# ---------------------------------------------------------------------------------------------
# 4. /tmp
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_a_tmp_entry_needs_a_registered_shape_this_user_and_age() -> None:
    me = os.getuid()
    ok = sweep.classify_tmp("opentranscribe_test_ab12_x9z", me, _OLD, _LATE, me)
    assert ok.tier == sweep.TIER_A
    assert sweep.classify_tmp("opentranscribe_test_ab12_x9z", me + 1, _OLD, _LATE, me).tier is None
    assert sweep.classify_tmp("opentranscribe_test_ab12_x9z", me, _LATE - 1, _LATE, me).tier is None
    assert sweep.classify_tmp("opentranscribe_test_notes", me, _OLD, _LATE, me).tier is None
    assert sweep.classify_tmp("tmp.abc123", me, _OLD, _LATE, me).tier is None


@pytest.mark.unit
def test_the_tmp_plane_removes_only_old_registered_entries(tmp_path: Path) -> None:
    old_tree = tmp_path / "opentranscribe_test_aaaa1111"
    (old_tree / "data").mkdir(parents=True)
    young_tree = tmp_path / "opentranscribe_test_bbbb2222"
    young_tree.mkdir()
    stranger = tmp_path / "unrelated_dir"
    stranger.mkdir()
    os.utime(old_tree, (_OLD, _OLD))
    cutoff = int(time.time()) - 3600
    os.utime(young_tree, (cutoff + 10, cutoff + 10))

    plane = sweep.TmpPlane(tmp_path)
    findings = {f.name: f for f in plane.list_findings(cutoff)}
    assert set(findings) == {old_tree.name, young_tree.name}, "unregistered entries never listed"
    assert findings[old_tree.name].tier == sweep.TIER_A
    assert findings[young_tree.name].tier is None

    assert plane.delete_one(findings[old_tree.name], cutoff, include_tier_b=False)
    assert not old_tree.exists()
    assert not plane.delete_one(findings[young_tree.name], cutoff, include_tier_b=True)
    assert young_tree.exists() and stranger.exists()


@pytest.mark.unit
def test_the_tmp_plane_refuses_a_path_outside_its_root(tmp_path: Path) -> None:
    plane = sweep.TmpPlane(tmp_path / "root")
    forged = sweep.Finding("tmp", "../opentranscribe_test_aaaa1111", sweep.TIER_A, "forged")
    (tmp_path / "opentranscribe_test_aaaa1111").mkdir()
    assert not plane.delete_one(forged, _LATE, include_tier_b=True)
    assert (tmp_path / "opentranscribe_test_aaaa1111").exists()


# ---------------------------------------------------------------------------------------------
# 5. Docker: Tier B only, label AND shape, compose projects by label only
# ---------------------------------------------------------------------------------------------

_TEST_LABEL = {"ot-test-artifact": "1"}


@pytest.mark.unit
def test_a_labelled_and_shaped_container_is_tier_b_never_tier_a() -> None:
    finding = sweep.classify_docker(
        "container", "ot-shared-throwaway-pg-0123456789ab", _TEST_LABEL, _OLD, _LATE
    )
    assert finding.tier == sweep.TIER_B
    assert not sweep.selected(finding, include_tier_b=False), "never removed automatically"


@pytest.mark.unit
def test_a_shaped_container_without_a_test_label_is_refused() -> None:
    finding = sweep.classify_docker(
        "container", "ot-shared-throwaway-pg-0123456789ab", {}, _OLD, _LATE
    )
    assert finding.tier is None


@pytest.mark.unit
@pytest.mark.parametrize("name", ["cvat_server", "tritonserver", "otfresh-v060-postgres", "pg"])
def test_a_labelled_container_with_an_unregistered_name_is_refused(name: str) -> None:
    assert sweep.classify_docker("container", name, _TEST_LABEL, _OLD, _LATE).tier is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "project", ["otfresh-v060", "transcribe-app", "opentranscribe", "cvat", "otbench"]
)
def test_a_real_compose_project_is_refused_even_with_a_test_label(project: str) -> None:
    labels = {"com.docker.compose.project": project, **_TEST_LABEL}
    name = "ot-shared-throwaway-pg-0123456789ab"
    assert sweep.classify_docker("container", name, labels, _OLD, _LATE).tier is None


@pytest.mark.unit
def test_a_pytest_compose_namespace_is_tier_b() -> None:
    labels = {"com.docker.compose.project": "opentranscribe-pytest-" + "a" * 32}
    assert sweep.classify_docker("container", "anything-1", labels, _OLD, _LATE).tier == "B"


def _fake_docker(tmp_path: Path, inspect_json: dict[str, str], ps_ids: dict[str, str]) -> Path:
    """A ``docker`` stand-in that logs its argv and answers ps/inspect/rm."""
    log = tmp_path / "docker.log"
    script = tmp_path / "docker"
    cases = "\n".join(
        f'    *"inspect"*"{ref}"*) printf %s {repr(body)} ;;' for ref, body in inspect_json.items()
    )
    ps_cases = "\n".join(
        f'    "ps "*"{flt}"*) printf "%s\\n" {ids} ;;' for flt, ids in ps_ids.items()
    )
    script.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$*" in\n'
        f"{cases}\n"
        f"{ps_cases}\n"
        '    "rm -f "*|"network rm "*) exit 0 ;;\n'
        "    *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.mark.unit
def test_the_docker_plane_lists_only_by_label_and_removes_only_with_tier_b(tmp_path: Path) -> None:
    ok = (
        '{"Name": "/ot-shared-throwaway-pg-0123456789ab", "Created": "2001-09-09T01:46:40Z",'
        ' "Config": {"Labels": {"ot-throwaway-pg": "1"}}}'
    )
    stranger = (
        '{"Name": "/cvat_server", "Created": "2001-09-09T01:46:40Z",'
        ' "Config": {"Labels": {"com.docker.compose.project": "cvat"}}}'
    )
    docker = _fake_docker(
        tmp_path,
        {"id-ok": ok, "ot-shared-throwaway-pg-0123456789ab": ok, "id-cvat": stranger},
        {"label=ot-throwaway-pg=1": "id-ok", "label=com.docker.compose.project": "id-cvat"},
    )
    plane = sweep.DockerPlane(str(docker))
    findings = plane.list_findings(_LATE)
    names = {f.name: f for f in findings}
    assert "cvat_server" not in names, "another project's container is not even reported"
    assert names["ot-shared-throwaway-pg-0123456789ab"].tier == sweep.TIER_B

    target = names["ot-shared-throwaway-pg-0123456789ab"]
    assert not plane.delete_one(target, _LATE, include_tier_b=False)
    assert plane.delete_one(target, _LATE, include_tier_b=True)

    calls = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    listing = [c for c in calls if c.startswith(("ps ", "network ls"))]
    assert listing and all("--filter label=" in c for c in listing), calls
    removals = [c for c in calls if c.startswith(("rm ", "network rm"))]
    assert removals == ["rm -f ot-shared-throwaway-pg-0123456789ab"], calls


@pytest.mark.unit
def test_docker_timestamps_with_nanoseconds_parse() -> None:
    assert sweep._parse_docker_time("2001-09-09T01:46:40.123456789Z") == pytest.approx(
        1_000_000_000.123456
    )
    assert sweep._parse_docker_time("") is None
    assert sweep._parse_docker_time("garbage") is None


# ---------------------------------------------------------------------------------------------
# 6. Stack scoping
# ---------------------------------------------------------------------------------------------


def _ports(*triples: tuple[str, int, str]) -> list:
    return [sweep.ResolvedPort(v, p, s) for v, p, s in triples]


@pytest.mark.unit
def test_an_exported_isolated_postgres_with_a_default_opensearch_is_refused() -> None:
    """The exact accident: POSTGRES_PORT exported for the isolated stack, OPENSEARCH_PORT
    forgotten, so the sweep would read one stack's database and the other's indexes."""
    reason = sweep.port_scope_mismatch(
        _ports(
            ("POSTGRES_PORT", 5376, "env"),
            ("OPENSEARCH_PORT", 5180, "default"),
            ("MINIO_PORT", 5378, "env"),
        )
    )
    assert reason is not None and "OPENSEARCH_PORT=5180" in reason


@pytest.mark.unit
def test_a_consistently_offset_stack_and_the_default_stack_are_accepted() -> None:
    assert (
        sweep.port_scope_mismatch(
            _ports(
                ("POSTGRES_PORT", 5376, "env"),
                ("OPENSEARCH_PORT", 5380, "env"),
                ("MINIO_PORT", 5378, "env"),
            )
        )
        is None
    )
    assert (
        sweep.port_scope_mismatch(
            _ports(
                ("POSTGRES_PORT", 5176, "default"),
                ("OPENSEARCH_PORT", 5180, "dotenv"),
                ("MINIO_PORT", 5178, "default"),
            )
        )
        is None
    )


# ---------------------------------------------------------------------------------------------
# 7. Registration gate: every throwaway store a test mints has a registered shape
# ---------------------------------------------------------------------------------------------

#: Calls whose first argument (or ``index=``/``id=``/``prefix=`` keyword) names a store.
_INDEX_CALLS = {"create": "index", "put_pipeline": "id", "make_bucket": None}
_TMP_CALLS = {"mkdtemp", "mkstemp", "NamedTemporaryFile", "TemporaryDirectory"}
_SPEAKER_SETTING = "OPENSEARCH_SPEAKER_INDEX"


def _sample(node: ast.AST, bindings: dict[str, ast.AST]) -> str | None:
    """A concrete example of the string ``node`` evaluates to, or None if not static."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in bindings:
        return _sample(bindings[node.id], {k: v for k, v in bindings.items() if k != node.id})
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
                continue
            assert isinstance(value, ast.FormattedValue)
            part = _sample(value.value, bindings)
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)
    # uuid4().hex[:N]
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
        upper = node.slice.upper
        if (
            isinstance(node.value, ast.Attribute)
            and node.value.attr == "hex"
            and isinstance(upper, ast.Constant)
            and isinstance(upper.value, int)
        ):
            return "a" * upper.value
    if isinstance(node, ast.Attribute) and node.attr == "hex":
        return "a" * 32
    if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in {
        "uuid4",
        "str",
    }:
        return "00000000-0000-4000-8000-000000000000"
    return None


def _bindings(scope: ast.AST) -> dict[str, ast.AST]:
    """Names assigned exactly once in ``scope`` (module or function body, not nested)."""
    counts: dict[str, int] = {}
    values: dict[str, ast.AST] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                counts[target.id] = counts.get(target.id, 0) + 1
                values[target.id] = node.value
    return {k: v for k, v in values.items() if counts[k] == 1}


def _store_arg(call: ast.Call) -> tuple[str, ast.AST | None]:
    """``(plane, name-expression)`` when ``call`` creates a store, else ``("", None)``."""
    func = call.func
    fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    kwargs = {k.arg: k.value for k in call.keywords if k.arg}
    first = call.args[0] if call.args else None
    if fname == "create" and isinstance(func, ast.Attribute):
        owner = func.value
        if isinstance(owner, ast.Attribute) and owner.attr == "indices":
            return "index", kwargs.get("index") or first
    if fname == "put_pipeline":
        return "pipeline", kwargs.get("id") or first
    if fname == "make_bucket":
        return "bucket", first or kwargs.get("bucket_name")
    if fname in _TMP_CALLS and "prefix" in kwargs:
        return "tmp-prefix", kwargs["prefix"]
    if fname == "setattr" and len(call.args) == 3:
        key = call.args[1]
        if isinstance(key, ast.Constant) and key.value == _SPEAKER_SETTING:
            return "speaker-base", call.args[2]
    return "", None


def minted_store_names(source: str) -> list[tuple[str, str, int]]:
    """``(plane, sample_name, lineno)`` for every store name a module statically mints.

    Each call is resolved once, against the bindings of its innermost enclosing function
    layered over the module's.
    """
    tree = ast.parse(source)
    module_bindings = _bindings(tree)
    found: list[tuple[str, str, int]] = []

    def visit(node: ast.AST, bindings: dict[str, ast.AST]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, {**bindings, **_bindings(child)})
                continue
            if isinstance(child, ast.Call):
                plane, arg = _store_arg(child)
                if arg is not None:
                    sample = _sample(arg, bindings)
                    if sample is not None:
                        found.append((plane, sample, child.lineno))
            visit(child, bindings)

    visit(tree, module_bindings)
    return found


#: Cheap pre-filter: a module that names none of these cannot mint a store.
_PREFILTER = re.compile(
    r"indices\.create|put_pipeline|make_bucket|prefix=|OPENSEARCH_SPEAKER_INDEX"
)


def _registered(plane: str, sample: str) -> bool:
    if plane == "index":
        return sweep.match_spec(sample, sweep.TEST_INDEX_SPECS) is not None
    if plane == "pipeline":
        return sweep.match_spec(sample, sweep.TEST_PIPELINE_SPECS) is not None
    if plane == "bucket":
        return sweep.match_spec(sample, sweep.TEST_BUCKET_SPECS) is not None
    if plane == "speaker-base":
        return all(
            sweep.match_spec(f"{sample}{suffix}", sweep.TEST_INDEX_SPECS) is not None
            for suffix in ("_v3", "_v4", "_v3_backup")
        )
    if plane == "tmp-prefix":
        # mkdtemp/mkstemp append 8 chars from [a-z0-9_]; a suffix (".json") may follow.
        return any(
            sweep.match_spec(f"{sample}abcd_123{ext}", sweep.TEST_TMP_SPECS) is not None
            for ext in ("", ".json")
        )
    raise AssertionError(plane)


#: Minted names deliberately NOT registered, with the reason. Each one must still exist.
_NOT_SWEPT: dict[str, str] = {
    # Created inside a pytest-managed tmp_path or a throwaway container, never on a live store.
}


@pytest.mark.unit
def test_every_store_name_minted_under_backend_tests_is_registered() -> None:
    unregistered: dict[str, str] = {}
    seen_planes: set[str] = set()
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8", errors="ignore")
        if not _PREFILTER.search(source):
            continue
        for plane, sample, lineno in minted_store_names(source):
            seen_planes.add(plane)
            key = f"{path.relative_to(_REPO_ROOT)}:{lineno}"
            if not _registered(plane, sample) and key not in _NOT_SWEPT:
                unregistered[key] = f"{plane} {sample!r}"
    assert {"index", "pipeline", "bucket", "tmp-prefix", "speaker-base"} <= seen_planes, (
        f"the scan stopped seeing a whole plane ({seen_planes}); it matches nothing there"
    )
    assert unregistered == {}, (
        "store names minted by tests with no shape in scripts/lib/test_artifact_sweep.py "
        f"(a killed run's leftover of each is unsweepable): {unregistered}"
    )


@pytest.mark.unit
def test_the_registration_scanner_fires_on_each_plane() -> None:
    """Guard the guard: synthetic unregistered names on every plane are found."""
    source = (
        "import tempfile, uuid\n"
        "def f(client, minio, settings, monkeypatch):\n"
        "    name = f'brand_new_{uuid.uuid4().hex[:12]}'\n"
        "    client.indices.create(index=name)\n"
        "    client.ingest.put_pipeline(id=f'new-pipe-{uuid.uuid4().hex[:8]}', body={})\n"
        "    minio.make_bucket(f'new-bucket-{uuid.uuid4().hex[:6]}')\n"
        "    tempfile.mkdtemp(prefix='new-tmp-')\n"
        "    monkeypatch.setattr(settings, 'OPENSEARCH_SPEAKER_INDEX', f'new_spk_{uuid.uuid4().hex[:10]}')\n"
    )
    minted = minted_store_names(source)
    assert {p for p, _, _ in minted} == {
        "index",
        "pipeline",
        "bucket",
        "tmp-prefix",
        "speaker-base",
    }
    assert not any(_registered(p, s) for p, s, _ in minted)

    registered = (
        "import uuid\n"
        "def g(client):\n"
        "    client.indices.create(index=f'test_summary_api_{uuid.uuid4().hex[:12]}')\n"
    )
    assert [(p, _registered(p, s)) for p, s, _ in minted_store_names(registered)] == [
        ("index", True)
    ]
